@echo off
echo ============================================
echo   INICIANDO - Financas da Casa Bot
echo ============================================
echo.

if not exist .env (
    echo ERRO: Arquivo .env nao encontrado!
    echo Copie o arquivo .env.template para .env
    echo e preencha as informacoes.
    pause
    exit /b 1
)

echo Iniciando o bot...
echo Para parar: feche esta janela ou pressione Ctrl+C
echo.
python bot_telegram.py
pause
