const APP = "http://127.0.0.1:8420";
let destino = "radio";
let formato = "mp3";
let escopo = "musica";
let urlAtual = "";
let tituloAba = "";     // nome da música tocando (texto)
let videoIdTocando = ""; // id exato da música tocando (via getVideoData)
let itensPrevia = [];

const $ = (id) => document.getElementById(id);

// Limpa o título da aba pra virar "Música - Artista".
function limparTituloAba(t) {
  return (t || "")
    .replace(/^\(\d+\)\s*/, "")                       // (3) de notificação
    .replace(/\s*[|\-–]\s*YouTube Music.*$/i, "")      // | ou - YouTube Music
    .replace(/\s*[|\-–]\s*YouTube.*$/i, "")
    .replace(/\s*[|\-–]\s*Spotify.*$/i, "")
    .replace(/\s*[|\-–]\s*SoundCloud.*$/i, "")
    .replace(/\s*[-–]\s*tocando agora.*$/i, "")
    .trim();
}

// ---------------- Abas ----------------
$("tabEnviar").onclick = () => trocarAba("enviar");
$("tabRadio").onclick = () => trocarAba("radio");
function trocarAba(qual) {
  $("tabEnviar").classList.toggle("sel", qual === "enviar");
  $("tabRadio").classList.toggle("sel", qual === "radio");
  $("abaEnviar").classList.toggle("hide", qual !== "enviar");
  $("abaRadioView").classList.toggle("hide", qual !== "radio");
  if (qual === "radio") iniciarAbaRadio();
}

// ---------------- Seletores ----------------
function setEscopo(e) {
  escopo = e;
  $("ePlay").classList.toggle("sel", e === "playlist");
  $("eMusica").classList.toggle("sel", e === "musica");
  const ehLista = itensPrevia.length > 1;
  const mostrar = (e === "musica" && ehLista);
  $("tocandoInfo").classList.toggle("hide", !mostrar);
  $("musicaSel").classList.toggle("hide", !mostrar);
  if (!mostrar) return;

  // Lista com o link EXATO de cada música (preciso). Já vem pré-selecionada
  // na faixa tocando quando dá pra detectar; você confirma ou troca.
  if (!$("musicaSel").options.length) {
    preencherSelect($("musicaSel"), itensPrevia.map((it, i) => ({ id: i, nome: it.titulo })), x => x.nome);
  }
  // 1º) match EXATO pelo id do vídeo (getVideoData). 2º) por nome.
  let idx = -1;
  if (videoIdTocando) idx = itensPrevia.findIndex(it => it.id === videoIdTocando);
  if (idx < 0 && tituloAba) {
    const alvo = tituloAba.toLowerCase();
    const chave = (alvo.split(" - ").pop() || alvo).trim();
    idx = itensPrevia.findIndex(it => {
      const t = (it.titulo || "").toLowerCase();
      return alvo.includes(t) || t.includes(chave) || chave.includes(t);
    });
  }
  if (idx >= 0) {
    $("musicaSel").value = idx;
    $("tocandoInfo").innerHTML = `🎧 Tocando: <b>${tituloAba || itensPrevia[idx].titulo}</b> — confirme/troque abaixo:`;
  } else if (tituloAba) {
    $("tocandoInfo").innerHTML = `🎧 Tocando: <b>${tituloAba}</b> — escolha na lista abaixo:`;
  } else {
    $("tocandoInfo").innerHTML = "Escolha a música que quer enviar:";
  }
}
$("ePlay").onclick = () => setEscopo("playlist");
$("eMusica").onclick = () => setEscopo("musica");

function setDestino(d) {
  destino = d;
  $("dRadio").classList.toggle("sel", d === "radio");
  $("dLocal").classList.toggle("sel", d === "local");
  $("boxRadioDest").classList.toggle("hide", d !== "radio");
  $("boxFormato").classList.toggle("hide", d === "radio");
}
$("dRadio").onclick = () => setDestino("radio");
$("dLocal").onclick = () => setDestino("local");

function setFormato(f) {
  formato = f;
  $("fMp3").classList.toggle("sel", f === "mp3");
  $("fMp4").classList.toggle("sel", f === "mp4");
}
$("fMp3").onclick = () => setFormato("mp3");
$("fMp4").onclick = () => setFormato("mp4");

function etapaDe(txt) {
  const t = txt.toLowerCase();
  if (t.includes("nivelando")) return { ic: "🔊", tit: "Nivelando o volume…" };
  if (txt.includes("⬆️") || t.includes("enviando")) return { ic: "📻", tit: "Enviando pra rádio…" };
  if (t.includes("extractaudio") || t.includes("convertendo")) return { ic: "🎵", tit: "Convertendo pra MP3…" };
  if (t.includes("search") || t.includes("downloading playlist") || t.includes("procurando")) return { ic: "🔎", tit: "Procurando a música…" };
  if (t.includes("[download]") || t.includes("baixando")) return { ic: "⬇️", tit: "Baixando…" };
  return null;
}

function preencherSelect(sel, itens, fmt) {
  sel.innerHTML = "";
  itens.forEach(it => {
    const o = document.createElement("option");
    o.value = it.id; o.textContent = fmt(it);
    sel.appendChild(o);
  });
}

async function pegarAba() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) return { url: "", title: "", musica: null, debug: "sem aba ativa" };
    let musica = null, debug = "";
    try {
      // world:"MAIN" roda no contexto da PÁGINA — só assim dá pra acessar a API
      // interna do player (getVideoData) e o mediaSession atualizado.
      const r = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: () => {
          // 1º) API do player do YouTube/YT Music: video_id EXATO da faixa atual.
          try {
            const p = document.getElementById("movie_player");
            if (p && typeof p.getVideoData === "function") {
              const d = p.getVideoData();
              if (d && d.video_id) return { videoId: d.video_id, titulo: d.title || "", artista: d.author || "" };
            }
          } catch (e) {}
          // 2º) mediaSession no contexto da página (atualizado).
          try {
            const m = navigator.mediaSession && navigator.mediaSession.metadata;
            if (m && m.title) return { videoId: "", titulo: m.title, artista: m.artist || "" };
          } catch (e) {}
          return { videoId: "", titulo: "", artista: "" };
        }
      });
      if (r && r[0]) { musica = r[0].result; debug = "exec ok: " + JSON.stringify(musica); }
      else debug = "exec sem resultado";
    } catch (e) { debug = "erro executeScript: " + (e && e.message ? e.message : String(e)); }
    return { url: tab.url || "", title: tab.title || "", musica, debug };
  } catch (e) { return { url: "", title: "", musica: null, debug: "erro tabs: " + (e && e.message ? e.message : String(e)) }; }
}

async function carregarPlaylists() {
  try {
    const d = await (await fetch(`${APP}/radio/playlists?estacao=${encodeURIComponent($("estacao").value)}`)).json();
    if (d.playlists) preencherSelect($("playlist"), d.playlists, p => `${p.name} (${p.num_songs})`);
  } catch (e) {}
}
$("estacao").onchange = carregarPlaylists;

// Detecta se o link é playlist ou música (sempre oferece "Só 1 música").
async function detectarTipo() {
  $("boxEscopo").classList.remove("hide");  // sempre mostra a escolha
  try {
    const d = await (await fetch(`${APP}/previa`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: urlAtual })
    })).json();
    itensPrevia = d.itens || [];
    $("musicaSel").innerHTML = "";  // repopula conforme o novo link
    if (d.erro) { $("previa").textContent = ""; setEscopo("musica"); return; }
    if (d.tipo === "playlist") {
      $("previa").innerHTML = `📃 <b>${d.titulo || "Playlist"}</b> — ${d.qtd} músicas`;
      setEscopo("playlist");
    } else {
      $("previa").innerHTML = `🎵 <b>${d.titulo || "Música"}</b>`;
      setEscopo("musica");
    }
  } catch (e) { $("previa").textContent = ""; setEscopo("musica"); }
}

async function iniciar() {
  const aba = await pegarAba();
  urlAtual = aba.url;
  $("dbg").textContent = "🔧 " + (aba.debug || "");  // diagnóstico (me reporte o que aparece)
  const mt = aba.musica;
  if (mt && mt.titulo) {
    videoIdTocando = mt.videoId || "";
    tituloAba = (mt.artista ? mt.artista + " - " : "") + mt.titulo;
  } else {
    videoIdTocando = "";
    tituloAba = limparTituloAba(aba.title);
  }
  $("url").textContent = urlAtual || "(nenhuma aba detectada)";
  try {
    const cfg = await (await fetch(`${APP}/radio/config`)).json();
    $("appOff").classList.add("hide");
    $("painel").classList.remove("hide");
    if (cfg.tem_key && cfg.estacoes.length) {
      preencherSelect($("estacao"), cfg.estacoes, e => e.name);
      preencherSelect($("estacao2"), cfg.estacoes, e => e.name);
      carregarPlaylists();
    }
    detectarTipo();
  } catch (e) {
    $("appOff").classList.remove("hide");
    $("painel").classList.add("hide");
  }
}
$("btnRetry").onclick = iniciar;

// ---------------- Enviar ----------------
$("btnEnviar").onclick = async () => {
  if (!urlAtual) { $("msg").textContent = "Nenhum link detectado."; $("msg").className = "msg bad"; return; }
  const b = $("btnEnviar");
  b.disabled = true; b.textContent = destino === "radio" ? "Enviando..." : "Baixando...";
  $("msg").textContent = ""; $("msg").className = "msg";
  try {
    // "A que está tocando" numa playlist: usa o LINK EXATO da música escolhida no seletor.
    let urls = [];
    if (escopo === "musica" && itensPrevia.length > 1) {
      const it = itensPrevia[$("musicaSel").value];
      if (!it || !it.url) {
        $("msg").textContent = "Escolha uma música na lista.";
        $("msg").className = "msg bad"; b.disabled = false; b.textContent = "Enviar"; return;
      }
      urls = [it.url];
    }
    const corpo = {
      url: urlAtual, formato, escopo, destino, urls,
      estacao: destino === "radio" ? $("estacao").value : null,
      playlist_id: destino === "radio" ? $("playlist").value : null
    };
    const r = await (await fetch(`${APP}/baixar`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo)
    })).json();
    if (r.erro) { $("msg").textContent = r.erro; $("msg").className = "msg bad"; b.disabled = false; b.textContent = "Enviar"; return; }
    b.disabled = false; b.textContent = "Enviar";
    $("msg").innerHTML = "pode fechar — continua no app.";
    $("prog").classList.remove("hide");
    $("console").innerHTML = ""; $("console").classList.add("hide"); $("fill").style.width = "0%";
    $("etapaMini").textContent = "⏳ Conectando…"; $("etapaSub").textContent = "";
    const cons = $("console");
    const logar = (txt, cls) => { if (!txt) return; const d = document.createElement("div"); if (cls) d.className = cls; d.textContent = txt; cons.appendChild(d); cons.scrollTop = cons.scrollHeight; };
    const ev = new EventSource(`${APP}/eventos/${r.job_id}`);
    ev.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if ((msg.tipo === "log" || msg.tipo === "erro") && msg.texto) {
        logar(msg.texto, msg.tipo === "erro" ? "bad" : null);
        const et = etapaDe(msg.texto);
        if (et) $("etapaMini").textContent = et.ic + " " + et.tit;
        const mm = msg.texto.match(/⬆️\s*\((\d+)\/(\d+)\)\s*(.+)/);
        if (mm) $("etapaSub").textContent = `Música ${mm[1]} de ${mm[2]}: ${mm[3]}`;
      }
      if (typeof msg.percent === "number") $("fill").style.width = msg.percent + "%";
      if (msg.tipo === "fim") {
        ev.close(); $("fill").style.width = "100%";
        if (msg.ok) { $("etapaMini").textContent = "✅ " + (destino === "radio" ? "Enviado pra rádio!" : "Salvo no Mac!"); $("etapaSub").textContent = msg.resumo || ""; }
        else { $("etapaMini").textContent = "⚠️ Terminou com erro"; $("etapaSub").textContent = "toque em 'ver detalhes'"; }
      }
    };
    ev.onerror = () => ev.close();
  } catch (e) {
    $("msg").textContent = "Erro: o app está aberto?"; $("msg").className = "msg bad";
    b.disabled = false; b.textContent = "Enviar";
  }
};

// ---------------- Aba Na Rádio ----------------
let arquivosRadio = [];
$("estacao2").onchange = carregarArquivosRadio;
$("buscaRadio").addEventListener("input", filtrarRadio);

async function iniciarAbaRadio() {
  if (!$("estacao2").options.length) return;
  carregarArquivosRadio();
}

async function carregarArquivosRadio() {
  const est = $("estacao2").value;
  $("listaRadio").innerHTML = '<div class="vazio">carregando...</div>';
  try {
    const d = await (await fetch(`${APP}/radio/arquivos?estacao=${encodeURIComponent(est)}`)).json();
    if (d.erro) { $("listaRadio").innerHTML = `<div class="vazio bad">${d.erro}</div>`; return; }
    arquivosRadio = d.arquivos || [];
    renderRadio();
  } catch (e) { $("listaRadio").innerHTML = '<div class="vazio bad">app fechado?</div>'; }
}

function renderRadio() {
  const est = $("estacao2").value;
  if (!arquivosRadio.length) { $("listaRadio").innerHTML = '<div class="vazio">nenhuma música</div>'; return; }
  $("listaRadio").innerHTML = "";
  arquivosRadio.forEach(a => {
    const nome = a.artist ? `${a.artist} — ${a.title}` : a.title;
    const row = document.createElement("div");
    row.className = "litem";
    row.innerHTML = `<button class="bi play">▶</button><span class="tit"></span><button class="bi del">🗑</button>`;
    row.querySelector(".tit").textContent = nome;
    row.querySelector(".play").onclick = () => {
      const p = $("player"); p.src = `${APP}/radio/play?estacao=${est}&id=${a.id}`; p.classList.remove("hide"); p.play();
    };
    row.querySelector(".del").onclick = async () => {
      if (!confirm("Excluir da rádio?\n\n" + nome)) return;
      const r = await (await fetch(`${APP}/radio/excluir`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ estacao: est, id: a.id }) })).json();
      if (r.ok) { arquivosRadio = arquivosRadio.filter(x => x.id !== a.id); renderRadio(); }
      else alert("Não consegui excluir: " + (r.erro || ""));
    };
    $("listaRadio").appendChild(row);
  });
}

function filtrarRadio() {
  const q = $("buscaRadio").value.toLowerCase();
  $("listaRadio").querySelectorAll(".litem").forEach(r => {
    r.style.display = r.querySelector(".tit").textContent.toLowerCase().includes(q) ? "" : "none";
  });
}

$("toggleDet").onclick = () => {
  const esc = $("console").classList.toggle("hide");
  $("toggleDet").textContent = esc ? "ver detalhes" : "esconder detalhes";
};

try { $("ver").textContent = "v" + chrome.runtime.getManifest().version; } catch (e) {}
setDestino("radio");
iniciar();
