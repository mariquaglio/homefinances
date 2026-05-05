@echo off
echo ============================================
echo   INSTALACAO - Financas da Casa Bot
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERRO: Python nao encontrado!
    echo Por favor instale o Python em python.org
    pause
    exit /b 1
)

echo Instalando dependencias...
pip install -r requirements.txt

echo.
echo ============================================
echo   Instalacao concluida!
echo   Agora configure o arquivo .env
echo   e depois rode 2_iniciar.bat
echo ============================================
pause
