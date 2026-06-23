#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baixar Músicas - App para baixar playlist completa online (MP3 ou MP4).

Abre uma interface no navegador. Você cola o link da playlist (YouTube, etc.),
escolhe MP3 (só áudio) ou MP4 (vídeo) e baixa a lista inteira de uma vez.

Backend: Python (biblioteca padrão) + yt-dlp + ffmpeg.
"""

import base64
import glob
import hashlib
import html
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse

# ----------------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------------
HOST = os.environ.get("BAIXAR_HOST", "127.0.0.1")   # 0.0.0.0 no servidor/Docker
PORT = int(os.environ.get("BAIXAR_PORT", "8420"))
VERSAO = "2.2"  # incrementar a cada alteração
# Login: se BAIXAR_SENHA estiver definida (no servidor), exige usuário+senha.
# Local (sem a variável) continua sem senha.
LOGIN_USUARIO = os.environ.get("BAIXAR_USUARIO", "realce")
LOGIN_SENHA = os.environ.get("BAIXAR_SENHA", "")
EXIGE_LOGIN = bool(LOGIN_SENHA)
# Modo online: esconde "Salvar no Mac" (não faz sentido em servidor remoto).
MODO_ONLINE = os.environ.get("BAIXAR_ONLINE", "") == "1"
PASTA_DOWNLOADS = Path.home() / "Downloads" / "Baixar Musicas"
PASTA_APP = Path(__file__).resolve().parent
# Cache PERMANENTE dos áudios de prévia (pré-escuta). Fica salvo até você limpar.
PREVIA_DIR = PASTA_APP / "cache_audio"
# Playlists salvas para acompanhar músicas novas.
SALVAS_PATH = PASTA_APP / "playlists_salvas.json"
# Áudios de locução/vinheta gerados (TTS). Temporários por sessão.
TTS_DIR = PASTA_APP / "vinhetas_geradas"
# Log de erros persistente (#3).
ERRO_LOG = PASTA_APP / "erros.log"


def registrar_erro(contexto, exc=None):
    """Grava erro no arquivo de log com data/hora (para diagnóstico)."""
    try:
        agora = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(ERRO_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{agora}] {contexto}\n")
            if exc is not None:
                f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:  # noqa: BLE001
        pass

# Tipo de conteúdo por extensão de áudio.
TIPOS_AUDIO = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".webm": "audio/webm",
               ".opus": "audio/ogg", ".ogg": "audio/ogg", ".aac": "audio/aac"}

# Cada job guarda uma fila de mensagens de progresso e o estado do processo.
JOBS = {}
JOBS_LOCK = threading.Lock()


def localizar_executavel(nome):
    """Acha o caminho do binário (yt-dlp / ffmpeg), inclusive em /opt/homebrew."""
    caminho = shutil.which(nome)
    if caminho:
        return caminho
    for base in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
        tentativa = os.path.join(base, nome)
        if os.path.exists(tentativa):
            return tentativa
    return None


YTDLP = localizar_executavel("yt-dlp")
FFMPEG = localizar_executavel("ffmpeg")
FFPROBE = localizar_executavel("ffprobe")
BREW = localizar_executavel("brew")


def versao_ytdlp():
    try:
        return subprocess.run([YTDLP, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


def atualizar_ytdlp():
    """Atualiza o yt-dlp (motor de download). Retorna (ok, mensagem)."""
    if not YTDLP:
        return False, "yt-dlp não encontrado."
    antes = versao_ytdlp()
    # 1ª tentativa: auto-update do próprio yt-dlp (binário standalone).
    try:
        r = subprocess.run([YTDLP, "-U"], capture_output=True, text=True, timeout=120)
        saida = (r.stdout + r.stderr).lower()
    except Exception:  # noqa: BLE001
        saida = "erro"
    # Se instalado via Homebrew, o -U não funciona; usa brew upgrade.
    if BREW and ("not writable" in saida or "brew" in saida or "homebrew" in saida or "can't" in saida):
        try:
            subprocess.run([BREW, "upgrade", "yt-dlp"], capture_output=True, text=True, timeout=300)
        except Exception:  # noqa: BLE001
            pass
    depois = versao_ytdlp()
    if depois != antes:
        return True, f"Atualizado: {antes} → {depois}"
    return True, f"Já está na versão mais recente ({depois})."


# ----------------------------------------------------------------------------
# Configuração da rádio (AzuraCast) — guardada em config.json local
# ----------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def carregar_config():
    cfg = {"base_url": "https://radio.lojasrealce.shop", "api_key": "", "elevenlabs_key": ""}
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    # No servidor, as chaves vêm de variáveis de ambiente (têm prioridade).
    if os.environ.get("AZURA_BASE_URL"):
        cfg["base_url"] = os.environ["AZURA_BASE_URL"]
    if os.environ.get("AZURA_API_KEY"):
        cfg["api_key"] = os.environ["AZURA_API_KEY"]
    if os.environ.get("ELEVENLABS_KEY"):
        cfg["elevenlabs_key"] = os.environ["ELEVENLABS_KEY"]
    return cfg


def salvar_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)  # só o dono lê/escreve (a key é sensível)
    except OSError:
        pass


CONFIG = carregar_config()


def api_request(metodo, caminho, corpo=None):
    """Chamada à API do AzuraCast. Retorna (ok, dados_ou_erro)."""
    base = (CONFIG.get("base_url") or "").rstrip("/")
    key = CONFIG.get("api_key") or ""
    if not base:
        return False, "URL da rádio não configurada."
    url = f"{base}{caminho}"
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo, headers={
        "X-API-Key": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = r.read().decode("utf-8")
            return True, (json.loads(txt) if txt.strip() else {})
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", "ignore")[:300]
        if e.code in (401, 403):
            return False, "API Key inválida ou sem permissão. Confira a chave."
        return False, f"Erro {e.code} da rádio: {detalhe}"
    except urllib.error.URLError as e:
        return False, f"Não consegui falar com a rádio: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return False, f"Falha na API: {e}"


def listar_estacoes():
    ok, dados = api_request("GET", "/api/stations")
    if not ok:
        return []
    return [{"id": s["id"], "name": s["name"]} for s in dados]


def listar_playlists(estacao_id):
    ok, dados = api_request("GET", f"/api/station/{int(estacao_id)}/playlists")
    if not ok:
        return False, dados
    return True, [{"id": p["id"], "name": p["name"],
                   "num_songs": p.get("num_songs", 0)} for p in dados]


def listar_arquivos_radio(estacao_id):
    """Lista as músicas já presentes na biblioteca da estação."""
    ok, dados = api_request("GET", f"/api/station/{int(estacao_id)}/files")
    if not ok:
        return False, dados
    itens = []
    for f in dados:
        itens.append({
            "id": f.get("id"),
            "artist": f.get("artist") or "",
            "title": f.get("title") or Path(f.get("path", "")).stem,
            "length_text": f.get("length_text") or "",
            "playlists": [p.get("name") for p in f.get("playlists", [])],
            "art": f.get("art") or "",
        })
    return True, itens


def baixar_audio_radio(estacao_id, file_id):
    """Busca o áudio de um arquivo da rádio (com a key) p/ repassar ao navegador."""
    base = (CONFIG.get("base_url") or "").rstrip("/")
    url = f"{base}/api/station/{int(estacao_id)}/file/{int(file_id)}/play"
    req = urllib.request.Request(url, headers={"X-API-Key": CONFIG.get("api_key", "")})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.headers.get("Content-Type", "audio/mpeg"), r.read()
    except Exception:  # noqa: BLE001
        return None, None


def excluir_arquivo_radio(estacao_id, file_id):
    ok, dados = api_request("DELETE", f"/api/station/{int(estacao_id)}/file/{int(file_id)}")
    return ok, dados


def mover_tudo_para_raiz(estacao_id):
    """Move todos os arquivos que estão em subpastas para a raiz da estação."""
    ok, dados = api_request("GET", f"/api/station/{int(estacao_id)}/files")
    if not ok:
        return False, str(dados)
    movidos, falhas = 0, 0
    for f in dados:
        p = f.get("path", "") or ""
        if "/" in p:  # está numa subpasta
            novo = p.rsplit("/", 1)[-1]
            okm, _ = api_request("PUT", f"/api/station/{int(estacao_id)}/files/rename",
                                 {"file": p, "newPath": novo})
            if okm:
                movidos += 1
            else:
                falhas += 1
    return True, {"movidos": movidos, "falhas": falhas}


# ----------------------------------------------------------------------------
# Locução / Vinhetas (ElevenLabs TTS)
# ----------------------------------------------------------------------------
ELEVEN_BASE = "https://api.elevenlabs.io/v1"


def eleven_listar_vozes():
    key = CONFIG.get("elevenlabs_key") or ""
    if not key:
        return False, "Configure a API Key do ElevenLabs."
    req = urllib.request.Request(f"{ELEVEN_BASE}/voices", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            dados = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "API Key do ElevenLabs inválida."
        return False, f"Erro {e.code} do ElevenLabs."
    except Exception as e:  # noqa: BLE001
        return False, f"Falha: {e}"
    vozes = []
    for v in dados.get("voices", []):
        lab = v.get("labels", {}) or {}
        vozes.append({
            "id": v["voice_id"],
            "nome": v.get("name", ""),
            "lang": lab.get("language", ""),
            "accent": lab.get("accent", ""),
            "preview": v.get("preview_url", ""),
            # Plano grátis só usa vozes "premade" via API.
            "free_ok": v.get("category", "") == "premade",
        })
    # Vozes usáveis (free) primeiro; PT antes; depois nome.
    vozes.sort(key=lambda x: (not x["free_ok"], x["lang"] != "pt", x["nome"].lower()))
    return True, vozes


def eleven_creditos():
    key = CONFIG.get("elevenlabs_key") or ""
    if not key:
        return None
    req = urllib.request.Request(f"{ELEVEN_BASE}/user/subscription", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read())
        return {"usados": d.get("character_count", 0), "limite": d.get("character_limit", 0)}
    except Exception:  # noqa: BLE001
        return None


# Perfis de entonação: quanto menor a "stability" e maior o "style", mais expressivo/animado.
PERFIS_VOZ = {
    "normal":    {"stability": 0.50, "similarity_boost": 0.75, "style": 0.00, "use_speaker_boost": True},
    "animado":   {"stability": 0.32, "similarity_boost": 0.80, "style": 0.45, "use_speaker_boost": True},
    "empolgado": {"stability": 0.18, "similarity_boost": 0.85, "style": 0.75, "use_speaker_boost": True},
}


def eleven_gerar(texto, voice_id, estilo="normal"):
    """Gera o MP3 da locução. Retorna (ok, caminho_ou_erro)."""
    key = CONFIG.get("elevenlabs_key") or ""
    if not key:
        return False, "Configure a API Key do ElevenLabs."
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps({
        "text": texto,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": PERFIS_VOZ.get(estilo, PERFIS_VOZ["normal"]),
    }).encode()
    req = urllib.request.Request(
        f"{ELEVEN_BASE}/text-to-speech/{voice_id}", data=body, method="POST",
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            audio = r.read()
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", "ignore")[:300]
        if e.code in (401, 403):
            return False, "API Key do ElevenLabs inválida ou sem permissão."
        if e.code == 422:
            return False, "Texto inválido ou voz não encontrada."
        if e.code == 402:
            return False, ("Essa voz é paga (biblioteca) e o plano grátis não permite usá-la. "
                           "Escolha uma voz SEM 🔒 (elas falam português com leve sotaque).")
        return False, f"Erro {e.code} do ElevenLabs: {detalhe}"
    except Exception as e:  # noqa: BLE001
        return False, f"Falha ao gerar: {e}"
    vid = uuid.uuid4().hex
    caminho = TTS_DIR / f"{vid}.mp3"
    caminho.write_bytes(audio)
    return True, str(caminho)


def escolher_pasta_nativa():
    """Abre o diálogo nativo do macOS para escolher uma pasta. Retorna caminho ou None."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Escolha a pasta para salvar a vinheta:")'],
            capture_output=True, text=True, timeout=180)
        caminho = out.stdout.strip()
        return caminho or None
    except Exception:  # noqa: BLE001
        return None


def escolher_arquivo_audio_nativo():
    """Diálogo nativo do macOS para escolher um arquivo de áudio (trilha de fundo)."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose file with prompt "Escolha a música de fundo:" '
             'of type {"mp3","m4a","wav","aac","mp4","public.audio"})'],
            capture_output=True, text=True, timeout=180)
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def mixar_spot(voz_path, musica_path):
    """Mixa a locução por cima de uma música de fundo (volume reduzido, intro e
    cauda musicais). Retorna (ok, caminho_novo_ou_erro)."""
    if not FFMPEG:
        return False, "ffmpeg não encontrado."
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    saida = TTS_DIR / f"{uuid.uuid4().hex}.mp3"
    # voz com 1.2s de intro musical + 2.5s de cauda; fundo a ~28% do volume.
    filtro = ("[0:a]adelay=1200|1200,apad=pad_dur=2.5[v];"
              "[1:a]volume=0.28[m];"
              "[v][m]amix=inputs=2:duration=first:dropout_transition=0[mix]")
    try:
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", str(voz_path), "-i", str(musica_path),
             "-filter_complex", filtro, "-map", "[mix]",
             "-ar", "44100", "-ac", "2", "-b:a", "192k", str(saida)],
            timeout=180, check=True)
        return True, str(saida)
    except Exception as e:  # noqa: BLE001
        registrar_erro("mixar_spot", e)
        return False, "Falha ao mixar o spot."


# ----------------------------------------------------------------------------
# Lógica de download
# ----------------------------------------------------------------------------
def montar_comando(url, formato, escopo, pasta_destino, urls=None):
    """Monta a linha de comando do yt-dlp conforme formato e escopo escolhidos."""
    base = [
        YTDLP,
        "--ignore-errors",         # se um item falhar, continua nos outros
        "--no-overwrites",         # não rebaixa o que já existe
        "--newline",               # uma linha por atualização de progresso
        "--no-color",
        "--restrict-filenames",    # nomes de arquivo seguros
        "--concurrent-fragments", "4",
    ]

    # Itens marcados na lista: cada URL é uma música (sem expandir playlist).
    if urls:
        base += ["--no-playlist",
                 "-o", str(pasta_destino / "%(title)s.%(ext)s")]
    elif escopo == "musica":
        # Apenas uma música/vídeo, mesmo que o link tenha uma playlist.
        base += [
            "--no-playlist",
            "-o", str(pasta_destino / "%(title)s.%(ext)s"),
        ]
    else:
        # Playlist inteira, organizada em subpasta com índice.
        base += [
            "--yes-playlist",
            "-o", str(pasta_destino / "%(playlist_title,channel,uploader|Playlist)s"
                      / "%(playlist_index|0)02d - %(title)s.%(ext)s"),
        ]

    if FFMPEG:
        base += ["--ffmpeg-location", os.path.dirname(FFMPEG)]

    if formato == "mp3":
        base += [
            "-f", "bestaudio/best",
            "-x",                          # extrai só o áudio
            "--audio-format", "mp3",
            "--audio-quality", "0",        # melhor qualidade
            "--embed-thumbnail",
            "--add-metadata",
        ]
    else:  # mp4
        base += [
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "--add-metadata",
        ]

    base += list(urls) if urls else [url]
    return base


def executar_download(job_id, url, formato, escopo, urls=None):
    """Roda o yt-dlp e empurra cada linha de saída para a fila do job."""
    job = JOBS[job_id]
    fila = job["fila"]
    pasta_destino = PASTA_DOWNLOADS
    pasta_destino.mkdir(parents=True, exist_ok=True)

    if not YTDLP:
        fila.put({"tipo": "erro", "texto": "yt-dlp não encontrado. Rode: brew install yt-dlp ffmpeg"})
        fila.put({"tipo": "fim", "ok": False})
        return

    cmd = montar_comando(url, formato, escopo, pasta_destino, urls)
    if urls:
        alvo = f"{len(urls)} música(s) selecionada(s)"
    else:
        alvo = "a playlist inteira" if escopo == "playlist" else "apenas uma música"
    fila.put({"tipo": "log", "texto": f"Iniciando download de {alvo} em {formato.upper()}..."})

    re_percent = re.compile(r"(\d{1,3}\.\d)%")
    re_item = re.compile(r"Downloading item (\d+) of (\d+)")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        job["proc"] = proc

        for linha in proc.stdout:
            if job.get("cancelado"):
                proc.terminate()
                break
            linha = linha.rstrip("\n")
            if not linha:
                continue

            msg = {"tipo": "log", "texto": linha}

            m_item = re_item.search(linha)
            if m_item:
                msg["item_atual"] = int(m_item.group(1))
                msg["item_total"] = int(m_item.group(2))

            m_pct = re_percent.search(linha)
            if m_pct:
                msg["percent"] = float(m_pct.group(1))

            fila.put(msg)

        proc.wait()
        if job.get("cancelado"):
            fila.put({"tipo": "log", "texto": "⛔ Cancelado pelo usuário."})
            fila.put({"tipo": "fim", "ok": False, "pasta": str(pasta_destino)})
            return
        ok = proc.returncode == 0
        fila.put({
            "tipo": "fim",
            "ok": ok,
            "pasta": str(pasta_destino),
        })
    except Exception as e:  # noqa: BLE001
        registrar_erro("executar_download", e)
        fila.put({"tipo": "erro", "texto": f"Falha: {e}"})
        fila.put({"tipo": "fim", "ok": False})


# Títulos resolvidos do Suno, por UUID. Persistido em disco para sobreviver a
# reinícios do app (a prévia preenche; o envio/baixa usa, mesmo depois).
SUNO_TITULOS_PATH = PASTA_APP / "suno_titulos.json"


def _carregar_suno_titulos():
    try:
        return json.loads(SUNO_TITULOS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


SUNO_TITULOS = _carregar_suno_titulos()


def resolver_suno(url):
    """Links do Suno: o yt-dlp pega só um placeholder de silêncio. Aqui achamos
    a URL real do MP3 e o título. Retorna dict ou None."""
    if "suno.com" not in url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            final = r.geturl()
            pagina = r.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    m = (re.search(r"/song/([a-f0-9-]{36})", final)
         or re.search(r"cdn1\.suno\.ai/([a-f0-9-]{36})\.mp3", pagina))
    if not m:
        return None
    uuid_ = m.group(1)
    tit = re.search(r'<meta property="og:title" content="([^"]+)"', pagina)
    img = re.search(r'<meta property="og:image" content="([^"]+)"', pagina)
    titulo = (tit.group(1).strip() if tit else "Suno").replace(" | Suno", "").strip()
    titulo = html.unescape(titulo)  # &amp; -> &, etc.
    SUNO_TITULOS[uuid_] = titulo
    try:
        SUNO_TITULOS_PATH.write_text(json.dumps(SUNO_TITULOS, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return {
        "audio_url": f"https://cdn1.suno.ai/{uuid_}.mp3",
        "titulo": titulo,
        "thumb": img.group(1) if img else "",
        "uuid": uuid_,
    }


_RE_UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"


def resolver_suno_itens(url):
    """Resolve link do Suno (1 música OU playlist/perfil). Retorna (titulo_pagina, [itens])."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            final = r.geturl()
            pagina = r.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None, []
    uuids = []
    for m in re.finditer(r"(?:/song/|cdn1\.suno\.ai/)(" + _RE_UUID + ")", final + " " + pagina):
        if m.group(1) not in uuids:
            uuids.append(m.group(1))
    if not uuids:
        return None, []
    tit = re.search(r'<meta property="og:title" content="([^"]+)"', pagina)
    titulo_pg = html.unescape(tit.group(1).strip().replace(" | Suno", "")) if tit else "Suno"
    itens = []
    for i, u in enumerate(uuids):
        # 1 música: usa o título da página. Vários: best-effort (título por faixa não é confiável no Suno).
        t = titulo_pg if len(uuids) == 1 else f"{titulo_pg} — faixa {i + 1}"
        SUNO_TITULOS[u] = t
        itens.append({"uuid": u, "titulo": t, "url": f"https://cdn1.suno.ai/{u}.mp3"})
    try:
        SUNO_TITULOS_PATH.write_text(json.dumps(SUNO_TITULOS, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return titulo_pg, itens


def resolver_url_se_preciso(url):
    """Troca um link do Suno pela URL direta do MP3. Outros links passam intactos.
    Garante que o envio/baixa funcione mesmo sem ter passado pela prévia."""
    if url and "suno.com" in url:
        s = resolver_suno(url)
        if s:
            return s["audio_url"]
    return url


def preparar_audio_previa(url):
    """Baixa só o áudio (rápido, sem reencode) p/ pré-escuta. Usa cache. Retorna caminho ou None."""
    if not YTDLP:
        return None
    PREVIA_DIR.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(url.encode()).hexdigest()
    existentes = glob.glob(str(PREVIA_DIR / f"{h}.*"))
    existentes = [e for e in existentes if not e.endswith(".part")]
    if existentes:
        return existentes[0]
    cmd = [YTDLP, "--no-playlist", "--no-warnings", "--no-color",
           "-f", "bestaudio[ext=m4a]/bestaudio/best",
           "-o", str(PREVIA_DIR / f"{h}.%(ext)s"), url]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    achados = [e for e in glob.glob(str(PREVIA_DIR / f"{h}.*")) if not e.endswith(".part")]
    return achados[0] if achados else None


def info_cache():
    """Tamanho e quantidade de arquivos no cache de prévia."""
    total, n = 0, 0
    if PREVIA_DIR.exists():
        for f in PREVIA_DIR.iterdir():
            if f.is_file():
                total += f.stat().st_size
                n += 1
    return {"arquivos": n, "mb": round(total / 1048576, 1)}


def limpar_cache():
    if PREVIA_DIR.exists():
        for f in PREVIA_DIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass


# ---- Playlists salvas (acompanhar músicas novas) ----
def carregar_salvas():
    if SALVAS_PATH.exists():
        try:
            return json.loads(SALVAS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return []


def gravar_salvas(lista):
    SALVAS_PATH.write_text(json.dumps(lista, indent=2, ensure_ascii=False), encoding="utf-8")


def ids_da_playlist(url):
    """Retorna (titulo, [ {id,titulo,url} ]) de uma playlist, rápido (flat)."""
    cmd = [YTDLP, "-J", "--flat-playlist", "--no-warnings", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        data = json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        return None, []
    entries = [e for e in (data.get("entries") or []) if e]
    itens = [{"id": str(e.get("id") or ""), "titulo": e.get("title") or "(sem título)",
              "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"} for e in entries]
    return (data.get("title") or "Playlist"), itens


def adicionar_salva(url):
    titulo, itens = ids_da_playlist(url)
    if titulo is None:
        return False, "Não consegui ler essa playlist."
    salvas = carregar_salvas()
    for s in salvas:
        if s["url"] == url:
            return False, "Essa playlist já está salva."
    salvas.append({
        "url": url, "titulo": titulo,
        "ids_vistos": [it["id"] for it in itens],
        "qtd": len(itens),
    })
    gravar_salvas(salvas)
    return True, f"'{titulo}' salva ({len(itens)} músicas)."


def remover_salva(url):
    salvas = [s for s in carregar_salvas() if s["url"] != url]
    gravar_salvas(salvas)
    return True


def verificar_salvas():
    """Para cada playlist salva, detecta músicas novas (não vistas antes)."""
    resultado = []
    for s in carregar_salvas():
        titulo, itens = ids_da_playlist(s["url"])
        if titulo is None:
            resultado.append({**s, "erro": True, "novas": []})
            continue
        vistos = set(s.get("ids_vistos", []))
        novas = [it for it in itens if it["id"] not in vistos]
        resultado.append({
            "url": s["url"], "titulo": titulo, "qtd": len(itens),
            "novas": novas, "n_novas": len(novas), "erro": False,
        })
    return resultado


def marcar_salva_vista(url):
    """Atualiza o snapshot da playlist (zera o 'novas')."""
    titulo, itens = ids_da_playlist(url)
    salvas = carregar_salvas()
    for s in salvas:
        if s["url"] == url and titulo is not None:
            s["ids_vistos"] = [it["id"] for it in itens]
            s["qtd"] = len(itens)
            s["titulo"] = titulo
    gravar_salvas(salvas)
    return True


def obter_previa(url):
    """Lê metadados do link (rápido, sem baixar) para montar o card de prévia."""
    # Suno: caso especial (o yt-dlp pegaria só silêncio). Suporta 1 música ou playlist.
    if "suno.com" in url:
        titulo_pg, itens_suno = resolver_suno_itens(url)
        if not itens_suno:
            return {"erro": "Não consegui ler esse link do Suno."}
        itens = [{"id": it["uuid"], "yt": False, "titulo": it["titulo"],
                  "duracao": None, "thumb": "", "url": it["url"]} for it in itens_suno]
        if len(itens) == 1:
            return {"tipo": "video", "titulo": itens[0]["titulo"], "canal": "Suno",
                    "duracao": None, "thumb": "", "tem_playlist": False, "itens": itens}
        return {"tipo": "playlist", "titulo": titulo_pg, "canal": "Suno",
                "qtd": len(itens), "thumb": "", "tem_playlist": True, "itens": itens}

    if not YTDLP:
        return {"erro": "yt-dlp não encontrado."}

    cmd = [YTDLP, "-J", "--flat-playlist", "--no-warnings", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return {"erro": "Demorou demais para ler o link."}
    except Exception as e:  # noqa: BLE001
        return {"erro": f"Falha ao ler o link: {e}"}

    if out.returncode != 0 or not out.stdout.strip():
        return {"erro": "Não consegui ler esse link. Confira se está correto."}

    try:
        data = json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        return {"erro": "Resposta inesperada ao ler o link."}

    def achar_thumb(obj):
        # IDs de vídeo do YouTube têm 11 caracteres -> miniatura limpa e garantida.
        vid = obj.get("id") or ""
        if isinstance(vid, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        # Caso geral: maior miniatura "de verdade" (ignora storyboards/sprites).
        reais = [
            t for t in (obj.get("thumbnails") or [])
            if t.get("url") and "storyboard" not in t["url"] and (t.get("width") or t.get("height"))
        ]
        if reais:
            reais.sort(key=lambda t: (t.get("width", 0) or 0) * (t.get("height", 0) or 0))
            return reais[-1]["url"]
        ths = obj.get("thumbnails") or []
        return ths[-1].get("url") if ths else None

    def item_de(e):
        vid = str(e.get("id") or "")
        eh_yt = bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", vid))
        return {
            "id": vid,
            "yt": eh_yt,  # se é YouTube, dá pra tocar com o player embutido
            "titulo": e.get("title") or "(sem título)",
            "duracao": e.get("duration"),
            "thumb": achar_thumb(e),
            "url": e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if eh_yt else None),
        }

    if data.get("_type") == "playlist" or data.get("entries") is not None:
        entries = [e for e in (data.get("entries") or []) if e]
        thumb = achar_thumb(data) or (achar_thumb(entries[0]) if entries else None)
        return {
            "tipo": "playlist",
            "titulo": data.get("title") or "Playlist",
            "canal": data.get("channel") or data.get("uploader") or "",
            "qtd": len(entries),
            "thumb": thumb,
            "tem_playlist": True,
            "itens": [item_de(e) for e in entries],
        }

    return {
        "tipo": "video",
        "titulo": data.get("title") or "Vídeo",
        "canal": data.get("channel") or data.get("uploader") or "",
        "duracao": data.get("duration"),
        "thumb": achar_thumb(data),
        # Se o link tem "list=", dá pra baixar a playlist toda também.
        "tem_playlist": "list=" in url,
        "itens": [item_de(data)],
    }


# ----------------------------------------------------------------------------
# Envio para a rádio (AzuraCast)
# ----------------------------------------------------------------------------
# Sufixos comuns que sujam o título vindo do YouTube.
_LIXO = re.compile(
    r"\s*[\(\[][^)\]]*("
    r"official|video|lyric|audio|hd|4k|clipe|oficial|visualizer|"
    r"music\s*video|color\s*coded|legendad|tradu|remaster|ao\s*vivo|live"
    r")[^)\]]*[\)\]]",
    re.IGNORECASE,
)


def limpar_titulo(texto):
    if not texto:
        return ""
    t = _LIXO.sub("", texto)
    t = re.sub(r"\s{2,}", " ", t).strip(" -–—_")
    return t.strip()


def separar_artista_titulo(tag_artist, tag_title, nome_arquivo):
    """Decide Artista/Título a partir das tags ID3 ou do nome, separando por ' - '."""
    artist = (tag_artist or "").strip()
    title = limpar_titulo(tag_title) or limpar_titulo(nome_arquivo)
    # Se não há artista mas o título é "Fulano - Música", separa.
    if not artist and " - " in title:
        esq, dir_ = title.split(" - ", 1)
        artist, title = esq.strip(), dir_.strip()
    return artist, title


def ler_tags_mp3(caminho):
    """Lê artist/title gravados no MP3 (via ffprobe)."""
    if not FFPROBE:
        return "", ""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", caminho],
            capture_output=True, text=True, timeout=20,
        )
        tags = (json.loads(out.stdout).get("format", {}) or {}).get("tags", {}) or {}
        tags = {k.lower(): v for k, v in tags.items()}
        return tags.get("artist", ""), tags.get("title", "")
    except Exception:  # noqa: BLE001
        return "", ""


def normalizar_mp3_radio(caminho):
    """Prepara o MP3 para a rádio: 44.1kHz CBR (AzuraCast processa) + volume
    nivelado por loudnorm/EBU R128 (#5), pra todas as músicas tocarem no mesmo nível."""
    if not FFMPEG:
        return
    tmp = caminho + ".fix.mp3"
    try:
        subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", caminho,
             "-map_metadata", "0",
             "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",  # volume parelho (streaming/rádio)
             "-ar", "44100", "-ac", "2", "-b:a", "192k", "-write_xing", "1", tmp],
            timeout=300, check=True)
        os.replace(tmp, caminho)
    except Exception as e:  # noqa: BLE001
        registrar_erro(f"normalizar_mp3_radio({caminho})", e)
        if os.path.exists(tmp):
            os.remove(tmp)


def enviar_arquivo_radio(estacao_id, caminho_mp3, artist, title, playlist_id):
    """Sobe um MP3 e associa à playlist. Retorna (ok, mensagem)."""
    nome_base = (f"{artist} - {title}" if artist else title) or Path(caminho_mp3).stem
    nome_seguro = re.sub(r'[\\/:*?"<>|]', "_", nome_base).strip()[:120] or "musica"
    path_remoto = f"{nome_seguro}.mp3"  # raiz da estação (sem subpasta)

    with open(caminho_mp3, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    ok, dados = api_request("POST", f"/api/station/{int(estacao_id)}/files",
                            {"path": path_remoto, "file": b64})
    if not ok:
        # Arquivo já existente na rádio costuma vir como erro 500/400 com "already exists".
        if isinstance(dados, str) and "exist" in dados.lower():
            return "existe", "já estava na rádio"
        return "erro", str(dados)

    file_id = dados.get("id")
    # Passo 2: setar Artista/Título e jogar na playlist escolhida.
    corpo = {"playlists": [{"id": int(playlist_id)}]}
    if artist:
        corpo["artist"] = artist
    if title:
        corpo["title"] = title
    ok2, dados2 = api_request("PUT", f"/api/station/{int(estacao_id)}/file/{file_id}", corpo)
    if not ok2:
        return "ok", "enviada (mas falhou ao ajustar playlist/tags)"
    return "ok", "ok"


def enviar_playlist_para_radio(job_id, url, escopo, estacao_id, playlist_id, urls=None):
    """Baixa em MP3 numa pasta temporária e envia cada música para a rádio."""
    job = JOBS[job_id]
    fila = job["fila"]

    if not CONFIG.get("api_key"):
        fila.put({"tipo": "erro", "texto": "Configure a API Key da rádio primeiro."})
        fila.put({"tipo": "fim", "ok": False})
        return

    tmp = Path(tempfile.mkdtemp(prefix="radio_"))
    fila.put({"tipo": "log", "texto": "Baixando músicas (MP3) para enviar à rádio..."})

    # Sempre MP3 para rádio. Nome temporário por índice+id (estável).
    cmd = [YTDLP, "--ignore-errors", "--newline", "--no-color", "--restrict-filenames",
           "--concurrent-fragments", "4",
           "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
           "--audio-quality", "0", "--add-metadata"]
    if FFMPEG:
        cmd += ["--ffmpeg-location", os.path.dirname(FFMPEG)]
    if urls:
        cmd += ["--no-playlist", "-o", str(tmp / "%(autonumber)03d-%(id)s.%(ext)s")]
        cmd += list(urls)
    else:
        cmd += ["--no-playlist"] if escopo == "musica" else ["--yes-playlist"]
        cmd += ["-o", str(tmp / "%(playlist_index|0)03d-%(id)s.%(ext)s"), url]

    re_percent = re.compile(r"(\d{1,3}\.\d)%")
    re_item = re.compile(r"Downloading item (\d+) of (\d+)")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        job["proc"] = proc
        for linha in proc.stdout:
            if job.get("cancelado"):
                proc.terminate()
                break
            linha = linha.rstrip("\n")
            if not linha:
                continue
            msg = {"tipo": "log", "texto": linha}
            m = re_item.search(linha)
            if m:
                msg["item_atual"], msg["item_total"] = int(m.group(1)), int(m.group(2))
            mp = re_percent.search(linha)
            if mp:
                # No modo rádio o download é só metade do trabalho (envio é a outra).
                msg["percent"] = float(mp.group(1)) / 2
            fila.put(msg)
        proc.wait()
        if job.get("cancelado"):
            fila.put({"tipo": "log", "texto": "⛔ Cancelado pelo usuário."})
            fila.put({"tipo": "fim", "ok": False, "radio": True, "resumo": "cancelado"})
            shutil.rmtree(tmp, ignore_errors=True)
            return
    except Exception as e:  # noqa: BLE001
        fila.put({"tipo": "erro", "texto": f"Falha no download: {e}"})
        fila.put({"tipo": "fim", "ok": False})
        shutil.rmtree(tmp, ignore_errors=True)
        return

    arquivos = sorted(glob.glob(str(tmp / "*.mp3")))
    total = len(arquivos)
    if total == 0:
        fila.put({"tipo": "erro", "texto": "Nenhuma música foi baixada."})
        fila.put({"tipo": "fim", "ok": False})
        shutil.rmtree(tmp, ignore_errors=True)
        return

    fila.put({"tipo": "log", "texto": f"Enviando {total} música(s) para a rádio..."})
    enviadas, pulados, falhas = 0, 0, 0
    for i, caminho in enumerate(arquivos, 1):
        if job.get("cancelado"):
            fila.put({"tipo": "log", "texto": "⛔ Cancelado pelo usuário."})
            break
        fila.put({"tipo": "log", "texto": f"🔊 ({i}/{total}) nivelando volume..."})
        normalizar_mp3_radio(caminho)  # 44.1kHz + loudnorm
        ta, tt = ler_tags_mp3(caminho)
        # Suno: nome do arquivo é "NNN-{uuid}"; o yt-dlp grava o UUID como título
        # nas tags, então o título resolvido na prévia TEM PRIORIDADE.
        stem = Path(caminho).stem
        uuid_ = stem.split("-", 1)[1] if "-" in stem else stem
        if uuid_ in SUNO_TITULOS:
            tt = SUNO_TITULOS[uuid_]
        artist, title = separar_artista_titulo(ta, tt, Path(caminho).stem)
        rotulo = f"{artist} - {title}" if artist else title
        fila.put({"tipo": "log", "texto": f"⬆️  ({i}/{total}) {rotulo}"})
        status, msg = enviar_arquivo_radio(estacao_id, caminho, artist, title, playlist_id)
        if status == "ok":
            enviadas += 1
            fila.put({"tipo": "log", "texto": f"   ✅ {rotulo}"})
        elif status == "existe":
            pulados += 1
            fila.put({"tipo": "log", "texto": f"   ⏭️  {rotulo}: já estava na rádio (pulada)"})
        else:
            falhas += 1
            fila.put({"tipo": "log", "texto": f"   ⚠️  {rotulo}: {msg}"})
        fila.put({"item_atual": i, "item_total": total,
                  "percent": 50 + (i / total) * 50, "tipo": "log", "texto": ""})

    shutil.rmtree(tmp, ignore_errors=True)
    partes = [f"{enviadas} enviada(s)"]
    if pulados:
        partes.append(f"{pulados} já existia(m)")
    if falhas:
        partes.append(f"{falhas} falha(s)")
    fila.put({
        "tipo": "fim",
        "ok": falhas == 0 and not job.get("cancelado"),
        "radio": True,
        "resumo": ", ".join(partes),
    })


# ----------------------------------------------------------------------------
# Servidor HTTP
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silencia o log padrão no terminal
        pass

    def _cors(self):
        # Permite que a extensão do Chrome (outra origem) fale com o app local.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _enviar(self, status, content_type, corpo):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(corpo)))
        self._cors()
        self.end_headers()
        self.wfile.write(corpo)

    def servir_audio(self, caminho):
        """Serve um arquivo de áudio com suporte a Range (seek no player)."""
        tamanho = os.path.getsize(caminho)
        ctype = TIPOS_AUDIO.get(Path(caminho).suffix.lower(), "application/octet-stream")
        faixa = self.headers.get("Range")
        ini, fim = 0, tamanho - 1
        if faixa and faixa.startswith("bytes="):
            partes = faixa.split("=", 1)[1].split("-")
            if partes[0]:
                ini = int(partes[0])
            if len(partes) > 1 and partes[1]:
                fim = int(partes[1])
            fim = min(fim, tamanho - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{tamanho}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fim - ini + 1))
        self.end_headers()
        try:
            with open(caminho, "rb") as f:
                f.seek(ini)
                restante = fim - ini + 1
                while restante > 0:
                    bloco = f.read(min(65536, restante))
                    if not bloco:
                        break
                    self.wfile.write(bloco)
                    restante -= len(bloco)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _checar_login(self):
        """Se EXIGE_LOGIN, valida usuário+senha (HTTP Basic). Retorna True se ok."""
        if not EXIGE_LOGIN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                usr, pwd = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)
                if usr == LOGIN_USUARIO and pwd == LOGIN_SENHA:
                    return True
            except Exception:  # noqa: BLE001
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Baixar Musicas"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._checar_login():
            return
        try:
            self._rotear_get()
        except Exception as e:  # noqa: BLE001
            registrar_erro(f"GET {self.path}", e)
            try:
                self._enviar(500, "text/plain; charset=utf-8",
                             "Erro interno no servidor (veja erros.log).".encode())
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):
        if not self._checar_login():
            return
        try:
            self._rotear_post()
        except Exception as e:  # noqa: BLE001
            registrar_erro(f"POST {self.path}", e)
            try:
                self._enviar(500, "application/json",
                             json.dumps({"erro": "Erro interno (veja erros.log)."}).encode())
            except Exception:  # noqa: BLE001
                pass

    def _rotear_get(self):
        rota = urlparse(self.path).path

        if rota == "/":
            corpo = (HTML.replace("{{VERSAO}}", VERSAO)
                         .replace("{{ONLINE}}", "1" if MODO_ONLINE else "")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(corpo)))
            self._cors()
            self.end_headers()
            self.wfile.write(corpo)
            return

        if rota.startswith("/eventos/"):
            self.stream_eventos(rota.rsplit("/", 1)[-1])
            return

        if rota == "/previa-audio":
            qs = urlparse(self.path).query
            from urllib.parse import unquote
            url = unquote(dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("url", ""))
            caminho = preparar_audio_previa(url) if url else None
            if not caminho:
                self._enviar(502, "text/plain; charset=utf-8", b"Nao consegui preparar o audio")
                return
            self.servir_audio(caminho)
            return

        if rota == "/radio/config":
            # Estado da configuração (não devolve a key inteira, só se existe).
            cfg = {
                "tem_key": bool(CONFIG.get("api_key")),
                "base_url": CONFIG.get("base_url", ""),
                "estacoes": listar_estacoes() if CONFIG.get("api_key") else [],
            }
            self._enviar(200, "application/json", json.dumps(cfg).encode())
            return

        if rota == "/radio/playlists":
            qs = urlparse(self.path).query
            estacao = dict(p.split("=", 1) for p in qs.split("&") if "=" in p).get("estacao", "")
            ok, dados = listar_playlists(estacao) if estacao else (False, "estação não informada")
            corpo = {"playlists": dados} if ok else {"erro": dados}
            self._enviar(200 if ok else 400, "application/json", json.dumps(corpo).encode())
            return

        if rota == "/radio/arquivos":
            qs = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&") if "=" in p)
            estacao = qs.get("estacao", "")
            ok, dados = listar_arquivos_radio(estacao) if estacao else (False, "estação não informada")
            corpo = {"arquivos": dados} if ok else {"erro": dados}
            self._enviar(200 if ok else 400, "application/json", json.dumps(corpo).encode())
            return

        if rota == "/cache/info":
            self._enviar(200, "application/json", json.dumps(info_cache()).encode())
            return

        if rota == "/salvas":
            salvas = [{"url": s["url"], "titulo": s["titulo"], "qtd": s.get("qtd", 0)}
                      for s in carregar_salvas()]
            self._enviar(200, "application/json", json.dumps({"salvas": salvas}).encode())
            return

        if rota == "/salvas/verificar":
            self._enviar(200, "application/json", json.dumps({"itens": verificar_salvas()}).encode())
            return

        if rota == "/tts/vozes":
            ok, vozes = eleven_listar_vozes()
            corpo = {"vozes": vozes, "creditos": eleven_creditos()} if ok else {"erro": vozes}
            self._enviar(200 if ok else 400, "application/json", json.dumps(corpo).encode())
            return

        if rota == "/tts/audio":
            qs = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&") if "=" in p)
            vid = re.sub(r"[^a-f0-9]", "", qs.get("id", ""))
            caminho = TTS_DIR / f"{vid}.mp3"
            if not vid or not caminho.exists():
                self._enviar(404, "text/plain; charset=utf-8", b"audio nao encontrado")
                return
            self.servir_audio(str(caminho))
            return

        if rota == "/radio/play":
            qs = dict(p.split("=", 1) for p in urlparse(self.path).query.split("&") if "=" in p)
            ctype, audio = baixar_audio_radio(qs.get("estacao", "0"), qs.get("id", "0"))
            if audio is None:
                self._enviar(502, "text/plain; charset=utf-8", b"Falha ao tocar")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(audio)))
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            self.wfile.write(audio)
            return

        self._enviar(404, "text/plain; charset=utf-8", b"Nao encontrado")

    def _rotear_post(self):
        rota = urlparse(self.path).path

        if rota.startswith("/cancelar/"):
            job_id = rota.rsplit("/", 1)[-1]
            job = JOBS.get(job_id)
            if job:
                job["cancelado"] = True
                proc = job.get("proc")
                if proc:
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass
            self._enviar(200, "application/json", json.dumps({"ok": True}).encode())
            return

        if rota == "/sistema/atualizar-ytdlp":
            ok, msg = atualizar_ytdlp()
            self._enviar(200, "application/json", json.dumps({"ok": ok, "msg": msg}).encode())
            return

        if rota == "/previa":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            url = (dados.get("url") or "").strip()
            if not url:
                self._enviar(400, "application/json", json.dumps({"erro": "Cole o link."}).encode())
                return
            resultado = obter_previa(url)
            self._enviar(200, "application/json", json.dumps(resultado).encode())
            return

        if rota == "/radio/salvar-key":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            key = (dados.get("api_key") or "").strip()
            base = (dados.get("base_url") or CONFIG.get("base_url") or "").strip()
            if not key:
                self._enviar(400, "application/json", json.dumps({"erro": "Cole a API Key."}).encode())
                return
            CONFIG["api_key"], CONFIG["base_url"] = key, base
            salvar_config(CONFIG)
            estacoes = listar_estacoes()
            self._enviar(200, "application/json", json.dumps(
                {"ok": bool(estacoes), "estacoes": estacoes,
                 "erro": "" if estacoes else "Key salva, mas não consegui listar estações."}).encode())
            return

        if rota == "/cache/limpar":
            limpar_cache()
            self._enviar(200, "application/json", json.dumps({"ok": True, **info_cache()}).encode())
            return

        if rota == "/tts/salvar-key":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            key = (dados.get("elevenlabs_key") or "").strip()
            if not key:
                self._enviar(400, "application/json", json.dumps({"erro": "Cole a API Key."}).encode())
                return
            CONFIG["elevenlabs_key"] = key
            salvar_config(CONFIG)
            ok, _ = eleven_listar_vozes()
            self._enviar(200, "application/json", json.dumps(
                {"ok": ok, "erro": "" if ok else "Key salva, mas inválida ao testar."}).encode())
            return

        if rota == "/tts/gerar":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            texto = (dados.get("texto") or "").strip()
            voz = (dados.get("voz") or "").strip()
            estilo = dados.get("estilo") if dados.get("estilo") in ("normal", "animado", "empolgado") else "normal"
            if not texto or not voz:
                self._enviar(400, "application/json", json.dumps({"erro": "Escreva o texto e escolha a voz."}).encode())
                return
            ok, res = eleven_gerar(texto, voz, estilo)
            if ok:
                corpo = {"ok": True, "id": Path(res).stem, "chars": len(texto), "creditos": eleven_creditos()}
            else:
                corpo = {"ok": False, "erro": res}
            self._enviar(200 if ok else 400, "application/json", json.dumps(corpo).encode())
            return

        if rota == "/tts/spot":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            vid = re.sub(r"[^a-f0-9]", "", dados.get("id", ""))
            origem = TTS_DIR / f"{vid}.mp3"
            if not vid or not origem.exists():
                self._enviar(400, "application/json", json.dumps({"erro": "Gere a vinheta primeiro."}).encode())
                return
            musica = escolher_arquivo_audio_nativo()
            if not musica:
                self._enviar(200, "application/json", json.dumps({"ok": False, "cancelado": True}).encode())
                return
            ok, res = mixar_spot(str(origem), musica)
            if ok:
                self._enviar(200, "application/json", json.dumps({"ok": True, "id": Path(res).stem}).encode())
            else:
                self._enviar(200, "application/json", json.dumps({"ok": False, "erro": res}).encode())
            return

        if rota == "/tts/salvar":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            vid = re.sub(r"[^a-f0-9]", "", dados.get("id", ""))
            nome = re.sub(r'[\\/:*?"<>|]', "_", (dados.get("nome") or "vinheta").strip())[:80] or "vinheta"
            origem = TTS_DIR / f"{vid}.mp3"
            if not vid or not origem.exists():
                self._enviar(400, "application/json", json.dumps({"erro": "Gere a vinheta primeiro."}).encode())
                return
            pasta = escolher_pasta_nativa()
            if not pasta:
                self._enviar(200, "application/json", json.dumps({"ok": False, "cancelado": True}).encode())
                return
            destino = os.path.join(pasta, f"{nome}.mp3")
            try:
                shutil.copy2(origem, destino)
                self._enviar(200, "application/json", json.dumps({"ok": True, "caminho": destino}).encode())
            except Exception as e:  # noqa: BLE001
                self._enviar(200, "application/json", json.dumps({"ok": False, "erro": str(e)}).encode())
            return

        if rota == "/tts/enviar-radio":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            vid = re.sub(r"[^a-f0-9]", "", dados.get("id", ""))
            nome = (dados.get("nome") or "Vinheta").strip()[:100] or "Vinheta"
            estacao, playlist_id = dados.get("estacao"), dados.get("playlist_id")
            origem = TTS_DIR / f"{vid}.mp3"
            if not vid or not origem.exists():
                self._enviar(400, "application/json", json.dumps({"erro": "Gere a vinheta primeiro."}).encode())
                return
            if not estacao or not playlist_id:
                self._enviar(400, "application/json", json.dumps({"erro": "Escolha estação e playlist."}).encode())
                return
            ok, msg = enviar_arquivo_radio(estacao, str(origem), "", nome, playlist_id)
            self._enviar(200 if ok else 400, "application/json",
                         json.dumps({"ok": bool(ok), "erro": "" if ok else str(msg)}).encode())
            return

        if rota in ("/salvas/adicionar", "/salvas/remover", "/salvas/marcar-visto"):
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            url = (dados.get("url") or "").strip()
            if not url:
                self._enviar(400, "application/json", json.dumps({"erro": "Faltou a url."}).encode())
                return
            if rota == "/salvas/adicionar":
                ok, msg = adicionar_salva(url)
            elif rota == "/salvas/remover":
                ok, msg = remover_salva(url), "removida"
            else:
                ok, msg = marcar_salva_vista(url), "ok"
            self._enviar(200 if ok else 400, "application/json",
                         json.dumps({"ok": bool(ok), "msg": msg if ok else "", "erro": "" if ok else msg}).encode())
            return

        if rota == "/radio/mover-raiz":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            estacao = dados.get("estacao")
            if not estacao:
                self._enviar(400, "application/json", json.dumps({"erro": "Faltou estação."}).encode())
                return
            ok, res = mover_tudo_para_raiz(estacao)
            corpo = {"ok": True, **res} if ok else {"ok": False, "erro": str(res)}
            self._enviar(200 if ok else 400, "application/json", json.dumps(corpo).encode())
            return

        if rota == "/radio/renomear":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            estacao, fid = dados.get("estacao"), dados.get("id")
            title = (dados.get("title") or "").strip()
            artist = (dados.get("artist") or "").strip()
            if not estacao or not fid or not title:
                self._enviar(400, "application/json", json.dumps({"erro": "Faltou estação/id/título."}).encode())
                return
            corpo = {"title": title, "artist": artist}
            ok, dadosr = api_request("PUT", f"/api/station/{int(estacao)}/file/{int(fid)}", corpo)
            self._enviar(200 if ok else 400, "application/json",
                         json.dumps({"ok": bool(ok), "erro": "" if ok else str(dadosr)}).encode())
            return

        if rota == "/radio/excluir":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            estacao, fid = dados.get("estacao"), dados.get("id")
            if not estacao or not fid:
                self._enviar(400, "application/json", json.dumps({"erro": "Faltou estação/id."}).encode())
                return
            ok, msg = excluir_arquivo_radio(estacao, fid)
            self._enviar(200 if ok else 400, "application/json",
                         json.dumps({"ok": ok, "erro": "" if ok else str(msg)}).encode())
            return

        if rota == "/baixar":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho) or b"{}")
            url = (dados.get("url") or "").strip()
            formato = dados.get("formato") if dados.get("formato") in ("mp3", "mp4") else "mp3"
            escopo = dados.get("escopo") if dados.get("escopo") in ("playlist", "musica") else "playlist"
            destino = dados.get("destino") if dados.get("destino") in ("local", "radio") else "local"
            estacao = dados.get("estacao")
            playlist_id = dados.get("playlist_id")
            # Lista de URLs específicas (itens marcados na lista). Se vier, manda só esses.
            urls = [u for u in (dados.get("urls") or []) if u]

            if not url and not urls:
                self._enviar(400, "application/json", json.dumps({"erro": "Cole o link da playlist."}).encode())
                return
            if destino == "radio" and (not estacao or not playlist_id):
                self._enviar(400, "application/json", json.dumps({"erro": "Escolha a estação e a playlist da rádio."}).encode())
                return

            # Suno: resolve o link pela URL direta do MP3 (robusto mesmo sem prévia).
            url = resolver_url_se_preciso(url)
            urls = [resolver_url_se_preciso(u) for u in urls]

            job_id = uuid.uuid4().hex
            with JOBS_LOCK:
                JOBS[job_id] = {"fila": queue.Queue(), "proc": None, "cancelado": False}

            if destino == "radio":
                alvo = threading.Thread(
                    target=enviar_playlist_para_radio,
                    args=(job_id, url, escopo, estacao, playlist_id, urls), daemon=True)
            else:
                alvo = threading.Thread(
                    target=executar_download,
                    args=(job_id, url, formato, escopo, urls), daemon=True)
            alvo.start()

            self._enviar(200, "application/json", json.dumps({"job_id": job_id}).encode())
            return

        self._enviar(404, "application/json", b'{"erro":"rota"}')

    def stream_eventos(self, job_id):
        """Server-Sent Events: envia o progresso do job em tempo real."""
        job = JOBS.get(job_id)
        if not job:
            self._enviar(404, "text/plain", b"job inexistente")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        fila = job["fila"]
        while True:
            try:
                msg = fila.get(timeout=30)
            except queue.Empty:
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                except (BrokenPipeError, ConnectionResetError):
                    break
            try:
                self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
            if msg.get("tipo") == "fim":
                break

        with JOBS_LOCK:
            JOBS.pop(job_id, None)


# ----------------------------------------------------------------------------
# Interface (HTML + CSS + JS embutidos)
# ----------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Baixar Músicas</title>
<style>
  :root { --bg:#0f1115; --card:#171a21; --line:#252a34; --txt:#e8ebf0; --mut:#8b93a3;
          --ac:#7c5cff; --ac2:#22c55e; --err:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,system-ui,Segoe UI,Roboto,sans-serif;
         background:radial-gradient(1200px 600px at 50% -10%, #1b1f2b, var(--bg));
         color:var(--txt); min-height:100vh; display:flex; align-items:flex-start;
         justify-content:center; padding:40px 16px; }
  .wrap { width:100%; max-width:680px; }
  h1 { font-size:26px; margin:0 0 4px; display:flex; align-items:center; gap:10px; }
  .sub { color:var(--mut); margin:0 0 24px; font-size:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px;
          padding:22px; margin-bottom:16px; }
  label { display:block; font-size:13px; color:var(--mut); margin-bottom:8px; }
  input[type=text] { width:100%; padding:14px 16px; border-radius:12px; border:1px solid var(--line);
          background:#0e1016; color:var(--txt); font-size:15px; outline:none; }
  input[type=text]:focus { border-color:var(--ac); }
  textarea { width:100%; padding:14px 16px; border-radius:12px; border:1px solid var(--line);
          background:#0e1016; color:var(--txt); font-size:15px; outline:none; resize:vertical;
          font-family:inherit; }
  textarea:focus { border-color:var(--ac); }
  .formatos { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:18px; }
  .fmt { cursor:pointer; border:2px solid var(--line); border-radius:14px; padding:16px;
         text-align:center; transition:.15s; background:#0e1016; }
  .fmt:hover { border-color:#3a4150; }
  .fmt.sel { border-color:var(--ac); background:#1a1530; }
  .fmt .big { font-size:20px; font-weight:700; }
  .fmt .small { font-size:12px; color:var(--mut); margin-top:4px; }
  .fmt .emoji { font-size:26px; }
  button.go { width:100%; margin-top:18px; padding:15px; border:0; border-radius:12px;
         background:linear-gradient(135deg,var(--ac),#9d7bff); color:#fff; font-size:16px;
         font-weight:700; cursor:pointer; }
  button.go:disabled { opacity:.5; cursor:not-allowed; }
  .barra { height:12px; background:#0e1016; border-radius:99px; overflow:hidden; border:1px solid var(--line); }
  .barra > div { height:100%; width:0%; background:linear-gradient(90deg,var(--ac),var(--ac2)); transition:width .2s; }
  .status { font-size:14px; margin:14px 0 10px; color:var(--mut); }
  .console { background:#0a0c10; border:1px solid var(--line); border-radius:12px; padding:12px;
         height:200px; overflow:auto; font-family:ui-monospace,Menlo,monospace; font-size:12px;
         color:#9fb0c3; white-space:pre-wrap; }
  .hide { display:none; }
  .ok { color:var(--ac2); } .bad { color:var(--err); }
  a { color:var(--ac); }
  /* Card de prévia ao lado do vídeo */
  .previa { display:flex; gap:14px; align-items:center; margin-top:16px; padding:12px;
            background:#0e1016; border:1px solid var(--line); border-radius:14px; }
  .thumbBox { position:relative; flex:0 0 auto; }
  .thumbBox img { width:150px; height:84px; object-fit:cover; border-radius:10px;
            background:#1b1f2b; display:block; }
  .badge { position:absolute; bottom:6px; right:6px; background:rgba(0,0,0,.78);
            color:#fff; font-size:11px; padding:2px 7px; border-radius:6px; font-weight:600; }
  .previaInfo { min-width:0; }
  .previaTitulo { font-weight:700; font-size:15px; line-height:1.3;
            display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .previaCanal { color:var(--mut); font-size:13px; margin-top:4px; }
  .previaMeta { color:var(--ac); font-size:13px; margin-top:6px; font-weight:600; }
  .previaCarregando { margin-top:16px; color:var(--mut); font-size:14px; }
  select { width:100%; padding:13px 14px; border-radius:11px; border:1px solid var(--line);
           background:#0e1016; color:var(--txt); font-size:15px; outline:none; }
  select:focus { border-color:var(--ac); }
  .goSec { padding:0 18px; border:0; border-radius:11px; background:var(--ac); color:#fff;
           font-weight:700; cursor:pointer; white-space:nowrap; }
  .dica { color:var(--mut); font-size:12px; margin-top:8px; }
  .dica.bad { color:var(--err); } .dica.ok { color:var(--ac2); }
  /* Abas */
  .tabs { display:flex; gap:8px; margin-bottom:18px; }
  .tab { flex:1; text-align:center; padding:12px; border-radius:12px; cursor:pointer;
         border:1px solid var(--line); background:var(--card); color:var(--mut); font-weight:600; }
  .tab.sel { color:#fff; border-color:var(--ac); background:#1a1530; }
  /* Lista de itens da playlist (com player + seleção) */
  .listaTopo { display:flex; align-items:center; justify-content:space-between; margin-top:18px; }
  .listaTopo .acoes { display:flex; gap:10px; font-size:13px; }
  .listaTopo a { cursor:pointer; }
  .lista { margin-top:10px; max-height:340px; overflow:auto; border:1px solid var(--line);
           border-radius:12px; }
  .litem { display:flex; align-items:center; gap:10px; padding:8px 10px; border-bottom:1px solid var(--line); }
  .litem:last-child { border-bottom:0; }
  .litem.off { opacity:.4; }
  .litem input[type=checkbox] { width:18px; height:18px; accent-color:var(--ac); flex:0 0 auto; cursor:pointer; }
  .litem .tit { flex:1; min-width:0; font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .litem .dur { color:var(--mut); font-size:12px; flex:0 0 auto; }
  .btnIcon { flex:0 0 auto; width:34px; height:34px; border-radius:9px; border:1px solid var(--line);
             background:#0e1016; color:var(--txt); cursor:pointer; font-size:15px; }
  .btnIcon:hover { border-color:var(--ac); }
  .btnIcon.del:hover { border-color:var(--err); color:var(--err); }
  .player { width:100%; margin-top:10px; }
  iframe.yt { width:100%; aspect-ratio:16/9; border:0; border-radius:12px; margin-top:10px; }
  .vazio { color:var(--mut); font-size:14px; padding:14px; text-align:center; }
  .vazio.bad { color:var(--err); }
  .cacheRow { margin-top:14px; font-size:12px; color:var(--mut); text-align:center; }
  .cacheRow a { cursor:pointer; margin-left:6px; }
  .badgeNum { background:var(--err); color:#fff; border-radius:99px; font-size:11px;
              padding:1px 7px; margin-left:4px; font-weight:700; }
  /* Progresso limpo (estilo Google) */
  .etapa { display:flex; align-items:center; gap:14px; margin-bottom:16px; }
  .etapaIcone { font-size:26px; width:48px; height:48px; flex:0 0 auto; border-radius:14px;
                background:#1a1530; display:flex; align-items:center; justify-content:center; }
  .etapaTitulo { font-size:16px; font-weight:700; }
  .etapaSub { font-size:13px; color:var(--mut); margin-top:2px; white-space:nowrap;
              overflow:hidden; text-overflow:ellipsis; }
  .progRodape { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:14px; }
  .progRodape a { font-size:12px; color:var(--mut); cursor:pointer; }
  .spin { display:inline-block; animation:gira 1s linear infinite; }
  @keyframes gira { to { transform:rotate(360deg); } }
  .litemSalva { padding:12px; border-bottom:1px solid var(--line); }
  .litemSalva:last-child { border-bottom:0; }
  .litemSalva .nome { font-weight:600; font-size:14px; }
  .litemSalva .meta { font-size:12px; color:var(--mut); margin-top:3px; }
  .litemSalva .novas { color:var(--ac2); font-weight:700; }
  .litemSalva .linhaBtns { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
  .miniBtn { font-size:12px; padding:6px 12px; border-radius:9px; border:1px solid var(--line);
             background:#0e1016; color:var(--txt); cursor:pointer; }
  .miniBtn.prim { background:var(--ac); border-color:var(--ac); color:#fff; font-weight:600; }
  .miniBtn.danger:hover { border-color:var(--err); color:var(--err); }
</style>
</head>
<body>
<div class="wrap">
  <h1>🎵 Baixar Músicas <span style="font-size:13px;color:var(--mut);font-weight:400">v{{VERSAO}}</span></h1>
  <p class="sub">Baixe playlists do YouTube ou envie direto pra rádio. Ouça antes de escolher.</p>

  <div class="tabs">
    <div class="tab sel" id="tabBaixar">⬇️ Baixar / Enviar</div>
    <div class="tab" id="tabRadio">📻 Na Rádio</div>
    <div class="tab" id="tabSalvas">📌 Salvas <span id="badgeSalvas" class="badgeNum hide">0</span></div>
    <div class="tab" id="tabTTS">🎙️ Vinhetas</div>
  </div>

  <div id="abaBaixar">
  <div class="card" id="cardForm">
    <label>Link do vídeo ou da playlist (YouTube, etc.)</label>
    <input type="text" id="url" placeholder="Cole aqui o link do vídeo ou da playlist...">

    <!-- Card de prévia (aparece ao colar o link) -->
    <div class="previa hide" id="previa">
      <div class="thumbBox">
        <img id="prevThumb" alt="">
        <span class="badge" id="prevBadge"></span>
      </div>
      <div class="previaInfo">
        <div class="previaTitulo" id="prevTitulo"></div>
        <div class="previaCanal" id="prevCanal"></div>
        <div class="previaMeta" id="prevMeta"></div>
        <button class="goSec hide" id="btnSalvarPlaylist" style="margin-top:8px">📌 Acompanhar playlist</button>
      </div>
    </div>
    <div class="previaCarregando hide" id="prevLoad">⏳ Lendo o link...</div>

    <!-- Player do YouTube (aparece ao clicar ▶ num item) -->
    <div id="ytPlayer"></div>

    <!-- Lista de músicas (ouvir / marcar quais enviar) -->
    <div id="boxLista" class="hide">
      <div class="listaTopo">
        <label style="margin:0" id="listaLabel">Músicas</label>
        <div class="acoes">
          <a id="marcarTodos">marcar todas</a>
          <a id="desmarcarTodos">desmarcar todas</a>
        </div>
      </div>
      <input type="text" id="buscaLista" placeholder="🔎 buscar nesta lista..." style="margin-top:8px">
      <div class="lista" id="lista"></div>
    </div>

    <!-- O que baixar -->
    <div id="boxEscopo" class="hide">
      <label style="margin-top:18px">O que você quer baixar?</label>
      <div class="formatos">
        <div class="fmt" data-esc="playlist" id="optPlaylist">
          <div class="emoji">📃</div>
          <div class="big">Playlist inteira</div>
          <div class="small" id="escPlayDesc">Todas as músicas da lista</div>
        </div>
        <div class="fmt sel" data-esc="musica" id="optMusica">
          <div class="emoji">🎵</div>
          <div class="big">Só esta música</div>
          <div class="small">Baixa apenas este item</div>
        </div>
      </div>
    </div>

    <!-- Para onde vai? -->
    <label style="margin-top:18px">Para onde vai?</label>
    <div class="formatos">
      <div class="fmt sel" data-dest="local" id="optLocal">
        <div class="emoji">💻</div>
        <div class="big">Salvar no Mac</div>
        <div class="small">Pasta Downloads</div>
      </div>
      <div class="fmt" data-dest="radio" id="optRadio">
        <div class="emoji">📻</div>
        <div class="big">Enviar pra Rádio</div>
        <div class="small">radio.lojasrealce.shop</div>
      </div>
    </div>

    <!-- Bloco da rádio (aparece ao escolher "Enviar pra Rádio") -->
    <div id="boxRadio" class="hide">
      <div id="boxKey" class="hide">
        <label style="margin-top:18px">API Key da rádio</label>
        <div style="display:flex; gap:8px">
          <input type="text" id="apiKey" placeholder="cole a API Key do AzuraCast aqui">
          <button class="goSec" id="btnKey">Salvar</button>
        </div>
        <div class="dica" id="keyMsg"></div>
      </div>
      <div id="boxRadioSel" class="hide">
        <label style="margin-top:18px">Estação</label>
        <select id="selEstacao"></select>
        <label style="margin-top:14px">Playlist de destino</label>
        <select id="selPlaylist"></select>
        <div class="dica">📻 Na rádio só faz sentido MP3 — o formato é travado em áudio.</div>
      </div>
    </div>

    <!-- Formato (só aparece no modo "Salvar no Mac") -->
    <div id="boxFormato">
      <label style="margin-top:18px">Quer em MP3 ou MP4?</label>
      <div class="formatos">
        <div class="fmt sel" data-fmt="mp3" id="optMp3">
          <div class="emoji">🎧</div>
          <div class="big">MP3</div>
          <div class="small">Só o áudio (música)</div>
        </div>
        <div class="fmt" data-fmt="mp4" id="optMp4">
          <div class="emoji">🎬</div>
          <div class="big">MP4</div>
          <div class="small">Vídeo completo</div>
        </div>
      </div>
    </div>

    <button class="go" id="btn">Baixar</button>
    <div class="cacheRow">💾 Cache de prévia: <b id="cacheInfo">…</b> <a id="btnLimparCache">limpar</a>
      &nbsp;·&nbsp; <a id="btnAtualizarMotor">🔄 atualizar motor de download</a>
      <span id="motorMsg"></span></div>
  </div>

  <div class="card hide" id="cardProg">
    <div class="etapa">
      <div class="etapaIcone" id="etapaIcone">⏳</div>
      <div style="min-width:0">
        <div class="etapaTitulo" id="status">Preparando…</div>
        <div class="etapaSub" id="etapaSub"></div>
      </div>
    </div>
    <div class="barra"><div id="fill"></div></div>
    <div class="progRodape">
      <button class="goSec hide" id="btnCancelar">⛔ Cancelar</button>
      <a id="toggleDetalhes">ver detalhes técnicos</a>
    </div>
    <div class="console hide" id="console"></div>
  </div>
  </div><!-- /abaBaixar -->

  <div id="abaRadio" class="hide">
    <div class="card">
      <div id="radioKeyAviso" class="hide">
        <label>API Key da rádio</label>
        <div style="display:flex; gap:8px">
          <input type="text" id="apiKey2" placeholder="cole a API Key do AzuraCast aqui">
          <button class="goSec" id="btnKey2">Salvar</button>
        </div>
        <div class="dica" id="keyMsg2"></div>
      </div>
      <div id="radioBiblioteca" class="hide">
        <label>Estação</label>
        <select id="selEstacao2"></select>
        <audio id="audioPlayer" class="player hide" controls></audio>
        <div class="listaTopo">
          <label style="margin:0" id="radioLabel">Músicas na rádio</label>
          <div class="acoes">
            <a id="moverRaiz" style="cursor:pointer">📂➡️ tirar tudo da pasta</a>
            <a id="recarregarRadio" style="cursor:pointer">recarregar</a>
          </div>
        </div>
        <input type="text" id="buscaRadio" placeholder="🔎 buscar música na rádio..." style="margin-top:8px">
        <div class="lista" id="listaRadio"><div class="vazio">carregando...</div></div>
      </div>
    </div>
  </div>

  <div id="abaSalvas" class="hide">
    <div class="card">
      <div class="listaTopo">
        <label style="margin:0">📌 Playlists que você acompanha</label>
        <a class="acoes" id="verificarSalvas" style="cursor:pointer">verificar agora</a>
      </div>
      <div class="dica">Salve uma playlist na aba "Baixar" (botão 📌). O app avisa aqui quando entrar música nova.</div>
      <div class="lista" id="listaSalvas" style="margin-top:12px"><div class="vazio">nenhuma playlist salva ainda</div></div>
    </div>
  </div>

  <div id="abaTTS" class="hide">
    <div class="card">
      <div id="ttsKeyAviso" class="hide">
        <label>API Key do ElevenLabs</label>
        <div style="display:flex; gap:8px">
          <input type="text" id="elevenKey" placeholder="cole a API Key do ElevenLabs">
          <button class="goSec" id="btnElevenKey">Salvar</button>
        </div>
        <div class="dica" id="elevenKeyMsg"></div>
      </div>
      <div id="ttsBox" class="hide">
        <label>Texto da locução / vinheta</label>
        <textarea id="ttsTexto" rows="4" placeholder="Ex: Você está ouvindo a Rádio Lojas Realce. A trilha sonora da sua loja!"></textarea>
        <div class="dica"><b id="ttsContador">0</b> caracteres &nbsp;·&nbsp; <span id="ttsCreditos"></span></div>
        <label style="margin-top:14px">Voz <span style="color:var(--mut)">(🔒 = só no plano pago; as sem cadeado falam PT com leve sotaque)</span></label>
        <div style="display:flex; gap:8px">
          <select id="ttsVoz"></select>
          <button class="goSec" id="btnAmostra" title="Ouvir amostra da voz (grátis)">▶ amostra</button>
        </div>
        <audio id="amostraPlayer" class="hide"></audio>
        <label style="margin-top:14px">Estilo da locução</label>
        <select id="ttsEstilo">
          <option value="normal">🗣️ Normal — institucional, calmo</option>
          <option value="animado">🎉 Animado — com energia</option>
          <option value="empolgado">⚡ Empolgado — spot de promoção</option>
        </select>
        <div class="dica">Dica: pra ficar mais animado, escreva com emoção — ex: "Imperdível! Só hoje, ofertas incríveis na Lojas Realce!"</div>
        <button class="go" id="btnGerarTTS">🎙️ Gerar áudio</button>
        <div class="dica" id="ttsMsg" style="text-align:center"></div>

        <div id="ttsResultado" class="hide">
          <audio id="ttsPlayer" class="player" controls style="margin-top:16px"></audio>
          <label style="margin-top:14px">Nome do arquivo</label>
          <input type="text" id="ttsNome" placeholder="vinheta_realce">
          <div class="linhaBtns" style="margin-top:12px">
            <button class="miniBtn" id="btnSpot">🎵 Adicionar música de fundo</button>
            <button class="miniBtn prim" id="btnSalvarPasta">💾 Salvar numa pasta do Mac</button>
          </div>
          <label style="margin-top:16px">Ou enviar pra rádio</label>
          <select id="ttsEstacao"></select>
          <select id="ttsPlaylist" style="margin-top:8px"></select>
          <button class="miniBtn prim" id="btnEnviarTTSRadio" style="margin-top:10px">📻 Enviar pra rádio</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
let formato = "mp3";
let escopo = "musica";

const fmtOpt = (id, f) => document.getElementById(id).onclick = () => {
  formato = f;
  document.getElementById("optMp3").classList.toggle("sel", f === "mp3");
  document.getElementById("optMp4").classList.toggle("sel", f === "mp4");
};
fmtOpt("optMp3", "mp3"); fmtOpt("optMp4", "mp4");

function setEscopo(e) {
  escopo = e;
  document.getElementById("optPlaylist").classList.toggle("sel", e === "playlist");
  document.getElementById("optMusica").classList.toggle("sel", e === "musica");
  atualizarBotao();
}
document.getElementById("optPlaylist").onclick = () => setEscopo("playlist");
document.getElementById("optMusica").onclick = () => setEscopo("musica");

function atualizarBotao() {
  const naRadio = (typeof destino !== "undefined" && destino === "radio");
  document.getElementById("btn").textContent = naRadio
    ? "Enviar pra Rádio"
    : (escopo === "playlist" ? "Baixar playlist" : "Baixar música");
}

// ---- Prévia do link (card ao lado do vídeo) ----
const inputUrl = document.getElementById("url");
const previa = document.getElementById("previa");
const prevLoad = document.getElementById("prevLoad");
const boxEscopo = document.getElementById("boxEscopo");
let timerPrevia = null, ultimaUrl = "";

function segParaTempo(s) {
  if (!s) return "";
  s = Math.round(s);
  const m = Math.floor(s / 60), seg = String(s % 60).padStart(2, "0");
  return ` · ${m}:${seg}`;
}

async function carregarPrevia() {
  const url = inputUrl.value.trim();
  if (!url || url === ultimaUrl) return;
  ultimaUrl = url;
  previa.classList.add("hide");
  prevLoad.classList.remove("hide");

  let data;
  try {
    const r = await fetch("/previa", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ url })
    });
    data = await r.json();
  } catch (e) { prevLoad.classList.add("hide"); return; }

  prevLoad.classList.add("hide");
  if (data.erro) { previa.classList.add("hide"); return; }

  document.getElementById("prevThumb").src = data.thumb || "";
  document.getElementById("prevTitulo").textContent = data.titulo || "";
  document.getElementById("prevCanal").textContent = data.canal || "";

  const btnSalvar = document.getElementById("btnSalvarPlaylist");
  if (data.tipo === "playlist") {
    document.getElementById("prevBadge").textContent = "PLAYLIST";
    document.getElementById("prevMeta").textContent = `📃 ${data.qtd} itens na lista`;
    document.getElementById("escPlayDesc").textContent = `Todas as ${data.qtd} músicas`;
    boxEscopo.classList.remove("hide");
    setEscopo("playlist");
    btnSalvar.classList.remove("hide");
    btnSalvar.textContent = "📌 Acompanhar playlist";
    btnSalvar.disabled = false;
    btnSalvar.dataset.url = url;
  } else {
    btnSalvar.classList.add("hide");
    document.getElementById("prevBadge").textContent = "VÍDEO" + segParaTempo(data.duracao);
    document.getElementById("prevMeta").textContent = data.tem_playlist
      ? "🎵 Música única (este link também tem playlist)" : "🎵 Música única";
    boxEscopo.classList.toggle("hide", !data.tem_playlist);
    setEscopo("musica");
  }
  renderLista(data.itens || []);
  previa.classList.remove("hide");
}

// ---- Lista de músicas com player (ouvir) e checkbox (enviar/excluir) ----
let itensPrevia = [];
const boxLista = document.getElementById("boxLista");
const listaEl = document.getElementById("lista");
const ytPlayer = document.getElementById("ytPlayer");

function renderLista(itens) {
  itensPrevia = itens;
  ytPlayer.innerHTML = "";
  if (!itens.length) { boxLista.classList.add("hide"); return; }
  document.getElementById("listaLabel").textContent =
    itens.length === 1 ? "Música" : `${itens.length} músicas — desmarque as que não quer`;
  listaEl.innerHTML = "";
  itens.forEach((it, i) => {
    const row = document.createElement("div");
    row.className = "litem";
    row.dataset.idx = i;
    const dur = it.duracao ? segParaTempo(it.duracao).replace(" · ", "") : "";
    row.innerHTML =
      `<input type="checkbox" checked>` +
      (it.url ? `<button class="btnIcon play" title="Ouvir prévia">▶</button>`
              : `<button class="btnIcon" title="Sem prévia" disabled>▶</button>`) +
      `<span class="tit"></span><span class="dur">${dur}</span>` +
      `<button class="btnIcon del" title="Remover da lista">🗑</button>`;
    row.querySelector(".tit").textContent = it.titulo;
    const chk = row.querySelector("input");
    chk.onchange = () => row.classList.toggle("off", !chk.checked);
    const play = row.querySelector(".play");
    if (play) play.onclick = (ev) => tocarPrevia(it.url, ev.currentTarget);
    row.querySelector(".del").onclick = () => { row.remove(); atualizarContadorLista(); };
    listaEl.appendChild(row);
  });
  boxLista.classList.remove("hide");
}

function atualizarContadorLista() {
  const n = listaEl.querySelectorAll(".litem").length;
  document.getElementById("listaLabel").textContent =
    n === 0 ? "Lista vazia — cole outro link" : (n === 1 ? "Música" : `${n} músicas — desmarque/remova as que não quer`);
}

let btnTocandoPrevia = null;
function tocarPrevia(url, btn) {
  if (btnTocandoPrevia) btnTocandoPrevia.textContent = "▶";
  btnTocandoPrevia = btn;
  btn.textContent = "⏳";
  ytPlayer.innerHTML =
    `<div class="dica" id="previaMsg">⏳ Preparando o áudio (alguns segundos)...</div>` +
    `<audio class="player" controls autoplay></audio>`;
  const a = ytPlayer.querySelector("audio");
  const msg = document.getElementById("previaMsg");
  a.src = "/previa-audio?url=" + encodeURIComponent(url);
  a.oncanplay = () => { msg.textContent = "🔊 Tocando prévia"; btn.textContent = "▶"; };
  a.onerror = () => {
    msg.innerHTML = "<span class='bad'>Não consegui pré-ouvir essa música.</span>";
    btn.textContent = "▶";
  };
  a.load();
}

function urlsSelecionadas() {
  const urls = [];
  listaEl.querySelectorAll(".litem").forEach(row => {
    const it = itensPrevia[+row.dataset.idx];
    if (row.querySelector("input").checked && it && it.url) urls.push(it.url);
  });
  return urls;
}

document.getElementById("marcarTodos").onclick = () =>
  listaEl.querySelectorAll(".litem").forEach(r => {
    r.querySelector("input").checked = true; r.classList.remove("off"); });
document.getElementById("desmarcarTodos").onclick = () =>
  listaEl.querySelectorAll(".litem").forEach(r => {
    r.querySelector("input").checked = false; r.classList.add("off"); });

inputUrl.addEventListener("input", () => {
  clearTimeout(timerPrevia);
  timerPrevia = setTimeout(carregarPrevia, 600);
});
inputUrl.addEventListener("blur", carregarPrevia);

// ---- Destino: Mac x Rádio ----
let destino = "local";
const boxRadio = document.getElementById("boxRadio");
const boxFormato = document.getElementById("boxFormato");
const boxKey = document.getElementById("boxKey");
const boxRadioSel = document.getElementById("boxRadioSel");
const selEstacao = document.getElementById("selEstacao");
const selPlaylist = document.getElementById("selPlaylist");
let radioPronto = false;

document.getElementById("optLocal").onclick = () => setDestino("local");
document.getElementById("optRadio").onclick = () => setDestino("radio");

function setDestino(d) {
  destino = d;
  document.getElementById("optLocal").classList.toggle("sel", d === "local");
  document.getElementById("optRadio").classList.toggle("sel", d === "radio");
  boxRadio.classList.toggle("hide", d !== "radio");
  boxFormato.classList.toggle("hide", d === "radio");  // rádio = sempre MP3
  if (d === "radio" && !radioPronto) carregarConfigRadio();
  atualizarBotao();
}

function preencherSelect(sel, itens, fmt) {
  sel.innerHTML = "";
  itens.forEach(it => {
    const o = document.createElement("option");
    o.value = it.id; o.textContent = fmt(it);
    sel.appendChild(o);
  });
}

async function carregarConfigRadio() {
  const cfg = await (await fetch("/radio/config")).json();
  if (cfg.tem_key && cfg.estacoes.length) {
    boxKey.classList.add("hide");
    boxRadioSel.classList.remove("hide");
    preencherSelect(selEstacao, cfg.estacoes, e => e.name);
    radioPronto = true;
    carregarPlaylists();
  } else {
    boxKey.classList.remove("hide");
    boxRadioSel.classList.add("hide");
  }
}

async function carregarPlaylists() {
  selPlaylist.innerHTML = "<option>carregando...</option>";
  const r = await fetch("/radio/playlists?estacao=" + encodeURIComponent(selEstacao.value));
  const d = await r.json();
  if (d.erro) { selPlaylist.innerHTML = "<option>erro: " + d.erro + "</option>"; return; }
  preencherSelect(selPlaylist, d.playlists, p => `${p.name} (${p.num_songs} músicas)`);
}
selEstacao.onchange = carregarPlaylists;

document.getElementById("btnKey").onclick = async () => {
  const key = document.getElementById("apiKey").value.trim();
  const msg = document.getElementById("keyMsg");
  if (!key) { msg.textContent = "Cole a API Key."; msg.className = "dica bad"; return; }
  msg.textContent = "Salvando..."; msg.className = "dica";
  const r = await fetch("/radio/salvar-key", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ api_key: key })
  });
  const d = await r.json();
  if (d.ok) { msg.textContent = "✅ Conectado!"; msg.className = "dica ok"; radioPronto = false; carregarConfigRadio(); }
  else { msg.textContent = d.erro || "Falhou."; msg.className = "dica bad"; }
};

// Modo online (rodando no servidor): esconde "Salvar no Mac".
const MODO_ONLINE = "{{ONLINE}}" === "1";
if (MODO_ONLINE) {
  document.getElementById("optLocal").style.display = "none";
  setDestino("radio");
}

atualizarBotao();

const btn = document.getElementById("btn");
const cons = document.getElementById("console");
const fill = document.getElementById("fill");
const status = document.getElementById("status");
const etapaIcone = document.getElementById("etapaIcone");
const etapaSub = document.getElementById("etapaSub");

// Traduz a linha técnica do yt-dlp numa etapa amigável.
function etapaDe(txt) {
  const t = txt.toLowerCase();
  if (t.includes("nivelando")) return { ic: "🔊", tit: "Nivelando o volume…" };
  if (txt.includes("⬆️") || t.includes("enviando")) return { ic: "📻", tit: "Enviando pra rádio…" };
  if (t.includes("extractaudio") || t.includes("convertendo")) return { ic: "🎵", tit: "Convertendo pra MP3…" };
  if (t.includes("search") || t.includes("downloading playlist") || t.includes("procurando")) return { ic: "🔎", tit: "Procurando a música…" };
  if (t.includes("[download]") || t.includes("baixando")) return { ic: "⬇️", tit: "Baixando…" };
  return null;
}

document.getElementById("toggleDetalhes").onclick = () => {
  const escondido = cons.classList.toggle("hide");
  document.getElementById("toggleDetalhes").textContent = escondido ? "ver detalhes técnicos" : "esconder detalhes";
};

function logar(txt, cls) {
  const span = document.createElement("div");
  if (cls) span.className = cls;
  span.textContent = txt;
  cons.appendChild(span);
  cons.scrollTop = cons.scrollHeight;
}

btn.onclick = async () => {
  const url = document.getElementById("url").value.trim();
  // Se a lista está aberta, manda só as músicas marcadas.
  const urls = boxLista.classList.contains("hide") ? [] : urlsSelecionadas();
  if (!url && !urls.length) { alert("Cole o link da playlist primeiro."); return; }
  if (!boxLista.classList.contains("hide") && urls.length === 0) {
    alert("Marque pelo menos uma música."); return;
  }
  if (destino === "radio") {
    if (!radioPronto) { alert("Configure a API Key da rádio primeiro."); return; }
    if (!selPlaylist.value || isNaN(+selPlaylist.value)) { alert("Escolha a playlist de destino."); return; }
  }

  btn.disabled = true;
  btn.textContent = destino === "radio" ? "Enviando..." : "Baixando...";
  document.getElementById("cardProg").classList.remove("hide");
  cons.innerHTML = ""; cons.classList.add("hide");
  document.getElementById("toggleDetalhes").textContent = "ver detalhes técnicos";
  fill.style.width = "0%";
  etapaIcone.innerHTML = "⏳"; status.textContent = "Conectando…"; etapaSub.textContent = "";

  let resp;
  try {
    resp = await fetch("/baixar", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ url, formato, escopo, destino, urls,
        estacao: destino === "radio" ? selEstacao.value : null,
        playlist_id: destino === "radio" ? selPlaylist.value : null })
    });
  } catch (e) { logar("Erro de conexão: " + e, "bad"); resetar(); return; }

  const data = await resp.json();
  if (data.erro) { logar(data.erro, "bad"); resetar(); return; }

  jobAtual = data.job_id;
  const bc = document.getElementById("btnCancelar");
  bc.classList.remove("hide"); bc.textContent = "⛔ Cancelar"; bc.disabled = false;

  const ev = new EventSource("/eventos/" + data.job_id);
  ev.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if ((msg.tipo === "log" || msg.tipo === "erro") && msg.texto) {
      logar(msg.texto, msg.tipo === "erro" ? "bad" : null);
      const et = etapaDe(msg.texto);
      if (et) { etapaIcone.innerHTML = et.ic; status.textContent = et.tit; }
      const mm = msg.texto.match(/⬆️\s*\((\d+)\/(\d+)\)\s*(.+)/);
      if (mm) etapaSub.textContent = `Música ${mm[1]} de ${mm[2]}: ${mm[3]}`;
    }
    if (msg.item_atual && msg.item_total) etapaSub.textContent = `Música ${msg.item_atual} de ${msg.item_total}`;
    if (typeof msg.percent === "number") fill.style.width = msg.percent + "%";
    if (msg.tipo === "fim") {
      ev.close();
      fill.style.width = "100%";
      if (msg.ok) {
        etapaIcone.innerHTML = "✅";
        status.textContent = msg.radio ? "Enviado pra rádio!" : "Concluído!";
        etapaSub.textContent = msg.radio ? (msg.resumo || "") : ("Salvo em: " + (msg.pasta || ""));
      } else {
        etapaIcone.innerHTML = "⚠️";
        status.textContent = msg.radio ? "Terminou com falhas" : "Terminou com erros";
        etapaSub.innerHTML = "<span class='bad'>toque em \"ver detalhes técnicos\"</span> " + (msg.resumo || "");
      }
      resetar();
    }
  };
  ev.onerror = () => { ev.close(); resetar(); };
};

function resetar() {
  btn.disabled = false;
  atualizarBotao();
  jobAtual = null;
  document.getElementById("btnCancelar").classList.add("hide");
}

// ========================= ABAS =========================
const abaBaixar = document.getElementById("abaBaixar");
const abaRadio = document.getElementById("abaRadio");
let radioJaCarregou = false;

document.getElementById("tabBaixar").onclick = () => trocarAba("baixar");
document.getElementById("tabRadio").onclick = () => trocarAba("radio");

const abaSalvas = document.getElementById("abaSalvas");
const abaTTS = document.getElementById("abaTTS");
document.getElementById("tabSalvas").onclick = () => trocarAba("salvas");
document.getElementById("tabTTS").onclick = () => trocarAba("tts");

function trocarAba(qual) {
  document.getElementById("tabBaixar").classList.toggle("sel", qual === "baixar");
  document.getElementById("tabRadio").classList.toggle("sel", qual === "radio");
  document.getElementById("tabSalvas").classList.toggle("sel", qual === "salvas");
  document.getElementById("tabTTS").classList.toggle("sel", qual === "tts");
  abaBaixar.classList.toggle("hide", qual !== "baixar");
  abaRadio.classList.toggle("hide", qual !== "radio");
  abaSalvas.classList.toggle("hide", qual !== "salvas");
  abaTTS.classList.toggle("hide", qual !== "tts");
  if (qual === "radio" && !radioJaCarregou) iniciarAbaRadio();
  if (qual === "salvas") carregarSalvas();
  if (qual === "tts" && !ttsJaCarregou) iniciarAbaTTS();
}

// ===================== ABA: NA RÁDIO =====================
const selEstacao2 = document.getElementById("selEstacao2");
const listaRadio = document.getElementById("listaRadio");
const audioPlayer = document.getElementById("audioPlayer");
const radioKeyAviso = document.getElementById("radioKeyAviso");
const radioBiblioteca = document.getElementById("radioBiblioteca");

async function iniciarAbaRadio() {
  const cfg = await (await fetch("/radio/config")).json();
  if (!cfg.tem_key || !cfg.estacoes.length) {
    radioKeyAviso.classList.remove("hide");
    radioBiblioteca.classList.add("hide");
    return;
  }
  radioKeyAviso.classList.add("hide");
  radioBiblioteca.classList.remove("hide");
  preencherSelect(selEstacao2, cfg.estacoes, e => e.name);
  radioJaCarregou = true;
  carregarArquivosRadio();
}

selEstacao2.onchange = carregarArquivosRadio;
document.getElementById("recarregarRadio").onclick = carregarArquivosRadio;

document.getElementById("moverRaiz").onclick = async () => {
  if (!confirm("Mover TODAS as músicas que estão em subpastas para a raiz desta estação?")) return;
  const lbl = document.getElementById("radioLabel");
  lbl.textContent = "movendo...";
  const d = await (await fetch("/radio/mover-raiz", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ estacao: selEstacao2.value })})).json();
  if (d.ok) alert(`✅ ${d.movidos} música(s) movida(s) pra raiz` + (d.falhas ? `, ${d.falhas} falha(s)` : ""));
  else alert("Não consegui mover: " + (d.erro || ""));
  carregarArquivosRadio();
};

document.getElementById("btnKey2").onclick = async () => {
  const key = document.getElementById("apiKey2").value.trim();
  const msg = document.getElementById("keyMsg2");
  if (!key) { msg.textContent = "Cole a API Key."; msg.className = "dica bad"; return; }
  msg.textContent = "Salvando..."; msg.className = "dica";
  const d = await (await fetch("/radio/salvar-key", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ api_key: key })
  })).json();
  if (d.ok) { msg.textContent = "✅ Conectado!"; msg.className = "dica ok"; radioJaCarregou = false; iniciarAbaRadio(); }
  else { msg.textContent = d.erro || "Falhou."; msg.className = "dica bad"; }
};

async function carregarArquivosRadio() {
  const est = selEstacao2.value;
  listaRadio.innerHTML = '<div class="vazio">carregando...</div>';
  const d = await (await fetch("/radio/arquivos?estacao=" + encodeURIComponent(est))).json();
  if (d.erro) { listaRadio.innerHTML = `<div class="vazio bad">${d.erro}</div>`; return; }
  document.getElementById("radioLabel").textContent = `${d.arquivos.length} músicas na rádio`;
  if (!d.arquivos.length) { listaRadio.innerHTML = '<div class="vazio">Nenhuma música ainda.</div>'; return; }
  listaRadio.innerHTML = "";
  d.arquivos.forEach(a => {
    const nome = a.artist ? `${a.artist} — ${a.title}` : a.title;
    const pls = a.playlists.length ? ` · ${a.playlists.join(", ")}` : "";
    const row = document.createElement("div");
    row.className = "litem";
    row.innerHTML =
      `<button class="btnIcon play" title="Ouvir">▶</button>` +
      `<span class="tit"></span><span class="dur">${a.length_text}${pls}</span>` +
      `<button class="btnIcon edit" title="Renomear">✏️</button>` +
      `<button class="btnIcon del" title="Excluir da rádio">🗑</button>`;
    row.querySelector(".tit").textContent = nome;
    row.querySelector(".play").onclick = () => {
      audioPlayer.src = `/radio/play?estacao=${est}&id=${a.id}`;
      audioPlayer.classList.remove("hide");
      audioPlayer.play();
    };
    row.querySelector(".edit").onclick = async () => {
      const novo = prompt("Novo título da música:", a.title || "");
      if (novo === null || !novo.trim()) return;
      const art = prompt("Artista (opcional):", a.artist || "");
      const r = await (await fetch("/radio/renomear", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ estacao: est, id: a.id, title: novo.trim(), artist: (art || "").trim() })
      })).json();
      if (r.ok) carregarArquivosRadio();
      else alert("Não consegui renomear: " + (r.erro || ""));
    };
    row.querySelector(".del").onclick = async () => {
      if (!confirm(`Excluir da rádio?\n\n${nome}`)) return;
      const r = await (await fetch("/radio/excluir", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ estacao: est, id: a.id })
      })).json();
      if (r.ok) row.remove();
      else alert("Não consegui excluir: " + (r.erro || ""));
    };
    listaRadio.appendChild(row);
  });
}

// ===================== CACHE DE PRÉVIA =====================
async function carregarCache() {
  try {
    const c = await (await fetch("/cache/info")).json();
    document.getElementById("cacheInfo").textContent = `${c.arquivos} áudios · ${c.mb} MB`;
  } catch (e) {}
}
document.getElementById("btnLimparCache").onclick = async () => {
  if (!confirm("Limpar o cache de prévia? As músicas precisarão ser preparadas de novo ao reouvir.")) return;
  const c = await (await fetch("/cache/limpar", {method: "POST"})).json();
  document.getElementById("cacheInfo").textContent = `${c.arquivos} áudios · ${c.mb} MB`;
};
carregarCache();

// ===================== SALVAR PLAYLIST =====================
document.getElementById("btnSalvarPlaylist").onclick = async (ev) => {
  const b = ev.currentTarget;
  const d = await (await fetch("/salvas/adicionar", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ url: b.dataset.url })
  })).json();
  b.textContent = d.ok ? "✅ Salva! (veja na aba Salvas)" : ("⚠️ " + (d.erro || "falhou"));
  b.disabled = d.ok;
  if (d.ok) verificarBadge();
};

// ===================== ABA: SALVAS =====================
const listaSalvas = document.getElementById("listaSalvas");
const badgeSalvas = document.getElementById("badgeSalvas");

async function carregarSalvas() {
  listaSalvas.innerHTML = '<div class="vazio">🔎 verificando músicas novas...</div>';
  const d = await (await fetch("/salvas/verificar")).json();
  renderSalvas(d.itens || []);
}
document.getElementById("verificarSalvas").onclick = carregarSalvas;

function renderSalvas(itens) {
  let totalNovas = 0;
  if (!itens.length) {
    listaSalvas.innerHTML = '<div class="vazio">nenhuma playlist salva ainda</div>';
    atualizarBadge(0);
    return;
  }
  listaSalvas.innerHTML = "";
  itens.forEach(it => {
    totalNovas += it.n_novas || 0;
    const row = document.createElement("div");
    row.className = "litemSalva";
    const novasTxt = it.erro ? "<span class='bad'>erro ao verificar</span>"
      : (it.n_novas > 0 ? `<span class='novas'>🆕 ${it.n_novas} música(s) nova(s)!</span>`
                        : "em dia");
    row.innerHTML =
      `<div class="nome"></div>` +
      `<div class="meta">${it.qtd} músicas · ${novasTxt}</div>` +
      `<div class="linhaBtns"></div>`;
    row.querySelector(".nome").textContent = it.titulo;
    const btns = row.querySelector(".linhaBtns");
    if (it.n_novas > 0) {
      const bNovas = document.createElement("button");
      bNovas.className = "miniBtn prim";
      bNovas.textContent = `Ver/baixar ${it.n_novas} nova(s)`;
      bNovas.onclick = () => abrirNovasNaAba(it.novas, it.url);
      btns.appendChild(bNovas);
      const bVisto = document.createElement("button");
      bVisto.className = "miniBtn";
      bVisto.textContent = "Marcar como visto";
      bVisto.onclick = async () => {
        await fetch("/salvas/marcar-visto", {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ url: it.url })});
        carregarSalvas();
      };
      btns.appendChild(bVisto);
    }
    const bRem = document.createElement("button");
    bRem.className = "miniBtn danger";
    bRem.textContent = "Remover";
    bRem.onclick = async () => {
      if (!confirm(`Parar de acompanhar?\n\n${it.titulo}`)) return;
      await fetch("/salvas/remover", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ url: it.url })});
      carregarSalvas();
    };
    btns.appendChild(bRem);
    listaSalvas.appendChild(row);
  });
  atualizarBadge(totalNovas);
}

// Joga as músicas novas na aba Baixar (com player e checkbox), pronto pra baixar/enviar.
function abrirNovasNaAba(novas, urlPlaylist) {
  document.getElementById("url").value = "";
  ultimaUrl = "";
  previa.classList.add("hide");
  const itens = novas.map(n => ({...n, yt: /^[A-Za-z0-9_-]{11}$/.test(n.id), duracao: null}));
  renderLista(itens);
  document.getElementById("listaLabel").textContent = `🆕 ${itens.length} música(s) nova(s) — escolha e baixe`;
  trocarAba("baixar");
  window.scrollTo(0, 0);
}

function atualizarBadge(n) {
  if (n > 0) { badgeSalvas.textContent = n; badgeSalvas.classList.remove("hide"); }
  else badgeSalvas.classList.add("hide");
}

// Verificação leve ao abrir o app: popula o badge sem travar (roda em background).
async function verificarBadge() {
  try {
    const s = await (await fetch("/salvas")).json();
    if (!s.salvas.length) return;
    const d = await (await fetch("/salvas/verificar")).json();
    atualizarBadge((d.itens || []).reduce((a, x) => a + (x.n_novas || 0), 0));
  } catch (e) {}
}
verificarBadge();

// ===================== EXTRAS (atualizar motor, cancelar, busca) =====================
let jobAtual = null;

// #1 Atualizar yt-dlp
document.getElementById("btnAtualizarMotor").onclick = async () => {
  const m = document.getElementById("motorMsg");
  m.textContent = " — atualizando (pode demorar)...";
  try {
    const d = await (await fetch("/sistema/atualizar-ytdlp", {method: "POST"})).json();
    m.textContent = " — " + (d.msg || "ok");
  } catch (e) { m.textContent = " — falhou"; }
};

// #6 Cancelar
document.getElementById("btnCancelar").onclick = async () => {
  if (jobAtual) { try { await fetch("/cancelar/" + jobAtual, {method: "POST"}); } catch (e) {} }
  document.getElementById("btnCancelar").textContent = "cancelando...";
};

// #7 Busca nas listas
function filtrarLista(input, container) {
  const q = input.value.toLowerCase();
  container.querySelectorAll(".litem, .litemSalva").forEach(r => {
    const t = (r.querySelector(".tit")?.textContent || r.querySelector(".nome")?.textContent || "").toLowerCase();
    r.style.display = t.includes(q) ? "" : "none";
  });
}
document.getElementById("buscaLista").addEventListener("input", e => filtrarLista(e.target, listaEl));
document.getElementById("buscaRadio").addEventListener("input", e => filtrarLista(e.target, listaRadio));

// ===================== ABA: VINHETAS (TTS) =====================
let ttsJaCarregou = false, ttsIdAtual = null, ttsPreviews = {};
const ttsTexto = document.getElementById("ttsTexto");
const ttsVoz = document.getElementById("ttsVoz");
const ttsEstacao = document.getElementById("ttsEstacao");
const ttsPlaylist = document.getElementById("ttsPlaylist");
const ttsMsg = document.getElementById("ttsMsg");

ttsTexto.addEventListener("input", () => {
  document.getElementById("ttsContador").textContent = ttsTexto.value.length;
});

function mostrarCreditos(c) {
  if (c) document.getElementById("ttsCreditos").textContent =
    `${c.usados}/${c.limite} caracteres usados no mês`;
}

async function iniciarAbaTTS() {
  const d = await (await fetch("/tts/vozes")).json();
  if (d.erro) {
    document.getElementById("ttsKeyAviso").classList.remove("hide");
    document.getElementById("ttsBox").classList.add("hide");
    return;
  }
  document.getElementById("ttsKeyAviso").classList.add("hide");
  document.getElementById("ttsBox").classList.remove("hide");
  ttsPreviews = {};
  d.vozes.forEach(v => { ttsPreviews[v.id] = v.preview; });
  preencherSelect(ttsVoz, d.vozes, v => (v.free_ok ? "" : "🔒 ") + (v.lang === "pt" ? "🇧🇷 " : "") + v.nome +
    (v.free_ok ? (v.accent && v.lang !== "pt" ? ` (sotaque ${v.accent})` : "") : " — plano pago"));
  mostrarCreditos(d.creditos);
  ttsJaCarregou = true;
  // Carrega estações/playlists pra opção "enviar pra rádio".
  const cfg = await (await fetch("/radio/config")).json();
  if (cfg.tem_key && cfg.estacoes.length) {
    preencherSelect(ttsEstacao, cfg.estacoes, e => e.name);
    carregarPlaylistsTTS();
  }
}
ttsEstacao.onchange = carregarPlaylistsTTS;

// Ouvir amostra da voz selecionada (grátis — não gasta créditos).
document.getElementById("btnAmostra").onclick = () => {
  const url = ttsPreviews[ttsVoz.value];
  const a = document.getElementById("amostraPlayer");
  if (!url) { ttsMsg.textContent = "Essa voz não tem amostra."; ttsMsg.className = "dica"; return; }
  a.src = url; a.play();
  ttsMsg.textContent = "🔊 Tocando amostra da voz";
  ttsMsg.className = "dica";
};

async function carregarPlaylistsTTS() {
  if (!ttsEstacao.value) return;
  const d = await (await fetch("/radio/playlists?estacao=" + encodeURIComponent(ttsEstacao.value))).json();
  if (d.playlists) preencherSelect(ttsPlaylist, d.playlists, p => `${p.name} (${p.num_songs})`);
}

document.getElementById("btnElevenKey").onclick = async () => {
  const key = document.getElementById("elevenKey").value.trim();
  const msg = document.getElementById("elevenKeyMsg");
  if (!key) { msg.textContent = "Cole a API Key."; msg.className = "dica bad"; return; }
  msg.textContent = "Salvando..."; msg.className = "dica";
  const d = await (await fetch("/tts/salvar-key", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ elevenlabs_key: key })})).json();
  if (d.ok) { msg.textContent = "✅ Conectado!"; msg.className = "dica ok"; ttsJaCarregou = false; iniciarAbaTTS(); }
  else { msg.textContent = d.erro || "Falhou."; msg.className = "dica bad"; }
};

document.getElementById("btnGerarTTS").onclick = async () => {
  const texto = ttsTexto.value.trim();
  if (!texto) { alert("Escreva o texto da vinheta."); return; }
  const b = document.getElementById("btnGerarTTS");
  b.disabled = true; b.textContent = "⏳ Gerando...";
  ttsMsg.textContent = "";
  const d = await (await fetch("/tts/gerar", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ texto, voz: ttsVoz.value, estilo: document.getElementById("ttsEstilo").value })})).json();
  b.disabled = false; b.textContent = "🎙️ Gerar áudio";
  if (!d.ok) { ttsMsg.textContent = d.erro || "Falhou."; ttsMsg.className = "dica bad"; return; }
  ttsIdAtual = d.id;
  mostrarCreditos(d.creditos);
  document.getElementById("ttsPlayer").src = "/tts/audio?id=" + d.id;
  document.getElementById("ttsResultado").classList.remove("hide");
  if (!document.getElementById("ttsNome").value)
    document.getElementById("ttsNome").value = texto.slice(0, 30).replace(/[^\w áéíóúâêôãõç-]/gi, "").trim() || "vinheta";
  ttsMsg.textContent = "";
};

document.getElementById("btnSpot").onclick = async () => {
  if (!ttsIdAtual) return;
  ttsMsg.textContent = "Escolha a música de fundo no seletor..."; ttsMsg.className = "dica";
  const d = await (await fetch("/tts/spot", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ id: ttsIdAtual })})).json();
  if (d.ok) {
    ttsIdAtual = d.id;
    const p = document.getElementById("ttsPlayer");
    p.src = "/tts/audio?id=" + d.id; p.load();
    ttsMsg.textContent = "✅ Spot com música de fundo pronto! Ouça acima."; ttsMsg.className = "dica ok";
  } else if (d.cancelado) { ttsMsg.textContent = "Cancelado."; ttsMsg.className = "dica"; }
  else { ttsMsg.textContent = d.erro || "Falhou."; ttsMsg.className = "dica bad"; }
};

document.getElementById("btnSalvarPasta").onclick = async () => {
  if (!ttsIdAtual) return;
  ttsMsg.textContent = "Abrindo seletor de pasta..."; ttsMsg.className = "dica";
  const d = await (await fetch("/tts/salvar", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ id: ttsIdAtual, nome: document.getElementById("ttsNome").value })})).json();
  if (d.ok) { ttsMsg.textContent = "✅ Salvo em: " + d.caminho; ttsMsg.className = "dica ok"; }
  else if (d.cancelado) { ttsMsg.textContent = "Cancelado."; ttsMsg.className = "dica"; }
  else { ttsMsg.textContent = d.erro || "Falhou."; ttsMsg.className = "dica bad"; }
};

document.getElementById("btnEnviarTTSRadio").onclick = async () => {
  if (!ttsIdAtual) return;
  if (!ttsPlaylist.value) { alert("Escolha a playlist."); return; }
  ttsMsg.textContent = "Enviando pra rádio..."; ttsMsg.className = "dica";
  const d = await (await fetch("/tts/enviar-radio", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ id: ttsIdAtual, nome: document.getElementById("ttsNome").value || "Vinheta",
      estacao: ttsEstacao.value, playlist_id: ttsPlaylist.value })})).json();
  if (d.ok) { ttsMsg.textContent = "✅ Enviado pra rádio!"; ttsMsg.className = "dica ok"; }
  else { ttsMsg.textContent = d.erro || "Falhou."; ttsMsg.className = "dica bad"; }
};
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# Inicialização
# ----------------------------------------------------------------------------
def main():
    if not YTDLP:
        print("\n[ERRO] yt-dlp não encontrado.")
        print("Instale com:  brew install yt-dlp ffmpeg\n")
        sys.exit(1)
    if not FFMPEG:
        print("\n[AVISO] ffmpeg não encontrado — conversão para MP3/MP4 pode falhar.")
        print("Instale com:  brew install ffmpeg\n")

    PASTA_DOWNLOADS.mkdir(parents=True, exist_ok=True)
    servidor = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"

    print("=" * 56)
    print("  🎵  Baixar Músicas — app rodando")
    print(f"  Abra no navegador:  {url}")
    print(f"  Downloads em:       {PASTA_DOWNLOADS}")
    print("  Para encerrar: feche esta janela ou aperte Ctrl+C")
    print("=" * 56)

    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
        servidor.shutdown()


if __name__ == "__main__":
    main()
