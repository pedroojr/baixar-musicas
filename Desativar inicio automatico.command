#!/bin/bash
# Desliga o início automático do Baixar Músicas.
PLIST="$HOME/Library/LaunchAgents/com.realce.baixarmusicas.plist"
launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
pkill -9 -f "Python app.py" 2>/dev/null
echo "✅ Início automático desativado. O app não sobe mais sozinho."
echo "   (Você ainda pode abrir manualmente com 'Iniciar Baixar Musicas.command'.)"
