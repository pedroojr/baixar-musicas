# 🎵 Baixar Músicas

App simples para **baixar playlist completa** de sites como YouTube. Ele pergunta se você quer **MP3** (só o áudio) ou **MP4** (vídeo) e baixa a lista inteira de uma vez.

## Como usar (jeito fácil)

1. Dê **dois cliques** no arquivo **`Iniciar Baixar Musicas.command`**.
2. O navegador abre sozinho com a tela do app.
3. **Cole o link da playlist**, escolha **MP3 ou MP4** e clique em **Baixar playlist**.
4. Os arquivos são salvos em: **`~/Downloads/Baixar Musicas/`**.

> Na primeira vez, o macOS pode pedir permissão para abrir o `.command`.
> Se bloquear: clique com o botão direito → **Abrir** → **Abrir**.

## Como usar (pelo terminal)

```bash
cd "~/APP - APLICATIVOS CRIADOS/Baixar Musicas"
python3 app.py
```

Depois abra no navegador: http://127.0.0.1:8420/

## O que precisa estar instalado

O app usa o `yt-dlp` (download) e o `ffmpeg` (conversão). Instale uma vez:

```bash
brew install yt-dlp ffmpeg
```

## 🎚️ Ouvir e escolher antes de baixar

Ao colar o link, aparece a **lista de músicas**. Em cada uma você pode:
- **▶ Ouvir** — toca o **áudio** ali na própria página (o app prepara o áudio em alguns segundos; clipes oficiais que bloqueiam o player do YouTube tocam normalmente aqui)
- **Marcar/desmarcar** quais quer baixar/enviar (use "marcar/desmarcar todas")
- **🗑 Remover da lista** — tira a música da lista de vez, deixando só as que você quer

Só as músicas que sobraram e estão marcadas são processadas.

**Cache de prévia:** os áudios que você ouve ficam salvos (pasta `cache_audio`), então reouvir é instantâneo. No rodapé do app você vê quanto ocupa e pode **limpar** quando quiser.

## 📌 Acompanhar playlists (avisa quando entra música nova)

- Ao ver uma playlist, clique em **📌 Acompanhar playlist**.
- Na aba **📌 Salvas** o app verifica (toda vez que você abre) se entraram **músicas novas** e mostra um aviso 🆕 com um número no topo.
- Clique em **Ver/baixar novas** pra trazer só as novas pra aba Baixar e baixá-las/enviá-las.
- **Marcar como visto** zera o aviso; **Remover** para de acompanhar.

## 📻 Aba "Na Rádio" — gerenciar o que já está no ar

Na aba **📻 Na Rádio** você vê todas as músicas já presentes na estação:
- Escolhe a **estação**
- **▶ Ouve** qualquer música (toca direto, via servidor local)
- **🗑 Exclui** o que não quiser mais (pede confirmação)

## 🎙️ Vinhetas / Locução (ElevenLabs)

Na aba **🎙️ Vinhetas** você cria áudios falados a partir de texto:
1. Escreve o texto (ex: *"Você está ouvindo a Rádio Lojas Realce..."*) — mostra o contador de caracteres
2. Escolhe a **voz** (as 🇧🇷 em português aparecem no topo) — botão **▶ amostra** toca a voz sem gastar créditos
3. Escolhe o **estilo**: Normal (institucional) · Animado · Empolgado (spot de promoção)
4. **🎙️ Gerar áudio** → ouve na hora
4. Dá um **nome** e:
   - **💾 Salva numa pasta do Mac** (abre o seletor de pastas do macOS), e/ou
   - **📻 Envia pra rádio** (escolhe estação + playlist)

> A API Key do ElevenLabs fica no `config.json`. **Custo:** o ElevenLabs cobra por caractere; o app mostra quanto você já usou no mês. O plano grátis dá 10.000 caracteres/mês.

## 📻 Enviar direto pra Rádio (AzuraCast)

O app pode enviar as músicas direto pra sua rádio em `radio.lojasrealce.shop`, sem upload manual:

1. Em **"Para onde vai?"**, escolha **📻 Enviar pra Rádio**
2. Na 1ª vez, cole a **API Key** do AzuraCast (gerada em Perfil → API Keys). Fica salva em `config.json`.
3. Escolha a **estação** e a **playlist de destino**
4. Cole o link e clique em **Enviar pra Rádio**

O app baixa em MP3, separa **Artista – Título** das tags, envia pro AzuraCast e joga na playlist escolhida. No modo rádio o formato é sempre MP3 (rádio não toca vídeo) e os arquivos **não** ficam no Mac.

> 🔒 **Segurança:** o `config.json` guarda sua API Key (acesso de escrita à rádio). Não compartilhe esse arquivo nem suba pra lugar público.

## ⚙️ Recursos extras

- **🔄 Atualizar motor de download** (rodapé): quando o YouTube quebrar o download, clique pra atualizar o yt-dlp.
- **🚀 Início automático**: rode `Ativar inicio automatico.command` **uma vez** → o app sobe sozinho sempre que ligar o Mac e se reinicia se travar. Pra desligar: `Desativar inicio automatico.command`.
- **⛔ Cancelar**: botão durante o download/envio pra interromper.
- **🔎 Busca**: campo pra filtrar nas listas (playlist e músicas da rádio).
- **🔊 Volume nivelado**: ao enviar pra rádio, todas as músicas saem no mesmo nível (loudnorm) e em 44.1kHz (compatível com o AzuraCast).
- **⏭️ Duplicatas**: se a música já está na rádio, o app pula e avisa (não duplica).
- **🌐 Várias fontes**: além do YouTube e Suno, funciona com SoundCloud, Vimeo, Bandcamp e centenas de sites (qualquer um suportado pelo yt-dlp). Spotify **não** dá (proteção da plataforma).
- **🎵 Spot com música de fundo** (aba Vinhetas): depois de gerar a locução, clique em "Adicionar música de fundo", escolha uma trilha e o app mixa a voz por cima.
- **📋 Log de erros**: erros ficam salvos em `erros.log` pra diagnóstico.

## Formatos

| Opção | O que baixa | Para quê |
|-------|-------------|----------|
| **MP3** | Só o áudio, melhor qualidade, com capa e nome da música | Ouvir música |
| **MP4** | Vídeo completo (imagem + som) | Assistir |

## Observações

- Baixa **a playlist inteira**. Se um vídeo falhar, ele pula e continua nos outros.
- Não rebaixa o que já existe na pasta (pode rodar de novo sem duplicar).
- Use apenas para conteúdo que você tem direito de baixar (suas músicas, domínio público, etc.).
