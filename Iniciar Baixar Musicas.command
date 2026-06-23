#!/bin/bash
# Clique duas vezes neste arquivo para abrir o app "Baixar Músicas".
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
python3 app.py
