#!/bin/bash
# Faz o app "Baixar Músicas" subir sozinho sempre que o Mac liga,
# e reiniciar automaticamente se travar. Rode UMA vez.
cd "$(dirname "$0")"

echo "Ativando início automático do Baixar Músicas..."

# Para qualquer instância manual (evita conflito de porta).
pkill -9 -f "Python app.py" 2>/dev/null
sleep 1

PLIST="$HOME/Library/LaunchAgents/com.realce.baixarmusicas.plist"
mkdir -p "$HOME/Library/LaunchAgents"
cp "com.realce.baixarmusicas.plist" "$PLIST"

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"
sleep 3

if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8420/ | grep -q 200; then
  echo "✅ Pronto! O app agora roda sozinho (mesmo após reiniciar o Mac)."
  echo "   Abrindo no navegador..."
  open "http://127.0.0.1:8420"
else
  echo "⚠️ O serviço foi instalado, mas o app não respondeu ainda."
  echo "   Veja o arquivo servico.log nesta pasta para detalhes."
fi
echo ""
echo "Para DESATIVAR depois, rode: Desativar inicio automatico.command"
