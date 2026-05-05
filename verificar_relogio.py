"""Verifica se o relógio do computador está correto comparando com servidores externos."""
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

print("Verificando horário do computador vs internet...\n")

try:
    req = urllib.request.urlopen("https://www.google.com", timeout=5)
    server_date_str = req.headers.get("Date")
    server_time = parsedate_to_datetime(server_date_str).astimezone(timezone.utc)
    local_time = datetime.now(timezone.utc)
    diff = (local_time - server_time).total_seconds()

    print(f"Horário local:    {local_time.strftime('%H:%M:%S')} UTC")
    print(f"Horário Google:   {server_time.strftime('%H:%M:%S')} UTC")
    print(f"Diferença:        {diff:.1f} segundos")

    if abs(diff) <= 60:
        print("\n✅ Relógio OK — diferença menor que 1 minuto.")
    else:
        print(f"\n⚠️  Relógio FORA DE SINCRONIA — diferença de {diff:.0f} segundos!")
        print("   Isso explica o erro do Google. Vamos corrigir.")

        import subprocess, sys
        # Tenta corrigir automaticamente via PowerShell
        correct_time = server_time.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n   Tentando corrigir para: {correct_time}")
        result = subprocess.run(
            ['powershell', '-Command', f'Set-Date -Date "{correct_time}"'],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print("✅ Horário corrigido com sucesso!")
        else:
            print("❌ Precisa de permissão de administrador para corrigir.")
            print("   Execute este script como administrador.")
except Exception as e:
    print(f"Erro: {e}")

input("\nPressione Enter para fechar...")
