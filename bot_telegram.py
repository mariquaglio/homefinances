"""
Bot Telegram - Finanças da Casa
Registra gastos via mensagem de texto e processa faturas PDF do cartão.
"""

import os
import re
import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from google.oauth2.service_account import Credentials
import pdfplumber
from dotenv import load_dotenv

load_dotenv()

# ── Configurações ──────────────────────────────────────────────────────────────
TOKEN               = os.getenv('TELEGRAM_TOKEN')
SPREADSHEET_ID      = os.getenv('SPREADSHEET_ID')
MARIANA_ID          = int(os.getenv('MARIANA_TELEGRAM_ID', '0'))
MARIDO_ID           = int(os.getenv('MARIDO_TELEGRAM_ID', '0'))
CREDENTIALS_FILE    = os.getenv('GOOGLE_CREDENTIALS_FILE', 'credentials.json')

# ── Categorias ─────────────────────────────────────────────────────────────────
CATEGORIAS = [
    'Mercado', 'Restaurante', 'Roupas', 'Natação', 'Escola',
    'Viagem', 'Casa Manutenção', 'Casa Prestadores', 'Carro',
    'Saúde', 'Lazer', 'Assinaturas', 'Imóvel', 'Outros'
]

PALAVRAS_CHAVE = {
    'Mercado':           ['mercado', 'supermercado', 'pão de açúcar', 'carrefour', 'extra',
                          'hortifruti', 'feira', 'atacadão', 'assaí', 'bistek'],
    'Restaurante':       ['restaurante', 'lanchonete', 'pizza', 'sushi', 'hamburguer', 'burger',
                          'ifood', 'delivery', 'cafe', 'café', 'padaria', 'bar', 'bistrô',
                          'churrascaria', 'pastelaria', 'espetinho'],
    'Roupas':            ['roupa', 'zara', 'hm', 'h&m', 'renner', 'riachuelo', 'farm', 'arezzo',
                          'sapato', 'tênis', 'vestuário', 'c&a', 'cea', 'lojas'],
    'Natação':           ['natação', 'natacao', 'piscina', 'swim', 'aula de natação'],
    'Escola':            ['escola', 'colégio', 'colegio', 'faculdade', 'curso',
                          'mensalidade', 'material escolar', 'livro escolar'],
    'Viagem':            ['hotel', 'airbnb', 'passagem', 'aéreo', 'aereo', 'viagem',
                          'booking', 'decolar', 'latam', 'gol', 'azul', 'voo'],
    'Casa Manutenção':   ['manutenção', 'manutencao', 'conserto', 'reparo', 'tinta',
                          'leroy', 'telha norte', 'ferragem', 'materiais'],
    'Casa Prestadores':  ['faxina', 'limpeza', 'jardineiro', 'pedreiro', 'eletricista',
                          'encanador', 'diarista', 'prestador', 'pintor'],
    'Carro':             ['gasolina', 'combustível', 'posto', 'ipva', 'seguro auto',
                          'manutenção carro', 'estacionamento', 'uber', 'táxi',
                          'pedágio', '99', 'oficina'],
    'Saúde':             ['farmácia', 'farmacia', 'médico', 'medico', 'dentista',
                          'hospital', 'clínica', 'clinica', 'exame', 'plano de saúde',
                          'drogaria', 'ultrafarma', 'droga raia'],
    'Lazer':             ['cinema', 'teatro', 'show', 'parque', 'clube', 'academia',
                          'lazer', 'diversão', 'ingresso', 'netflix', 'spotify'],
    'Assinaturas':       ['netflix', 'spotify', 'amazon prime', 'disney', 'globoplay',
                          'assinatura', 'hbo', 'paramount', 'apple tv'],
    'Imóvel':            ['imóvel', 'imovel', 'aluguel', 'condomínio', 'condominio',
                          'iptu', 'financiamento', 'escritura', 'cartório'],
}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Google Sheets ──────────────────────────────────────────────────────────────
def get_sheet():
    scopes = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


# ── Categorização ──────────────────────────────────────────────────────────────
def categorizar(descricao: str) -> str | None:
    desc_lower = descricao.lower()
    for cat, palavras in PALAVRAS_CHAVE.items():
        for palavra in palavras:
            if palavra in desc_lower:
                return cat
    return None


def sugerir_categoria(descricao: str) -> str:
    cat = categorizar(descricao)
    if cat:
        return cat
    # Pontuação parcial
    desc_lower = descricao.lower()
    scores: dict[str, int] = {}
    for cat, palavras in PALAVRAS_CHAVE.items():
        for palavra in palavras:
            for parte in palavra.split():
                if parte in desc_lower:
                    scores[cat] = scores.get(cat, 0) + 1
    if scores:
        return max(scores, key=scores.get)
    return 'Outros'


# ── Parser de mensagem ─────────────────────────────────────────────────────────
MARIDO_KEYWORDS = ['marido', 'dele', 'ele pagou', 'marido pagou']

def parse_gasto(texto: str, nome_padrao: str) -> dict | None:
    """
    Interpreta mensagens como:
      'restaurante 200'          → nome_padrao pagou 200
      'marido padaria 25,50'     → Marido pagou 25.50
      'mercado 150 ele'          → Marido pagou 150
    Retorna None se não parecer um gasto.
    """
    texto = texto.strip()
    pagador = nome_padrao

    # Verifica se é gasto do marido
    for kw in MARIDO_KEYWORDS:
        pattern = rf'^{re.escape(kw)}\s*[:\-]?\s*'
        if re.match(pattern, texto, re.IGNORECASE):
            pagador = 'Marido'
            texto = re.sub(pattern, '', texto, flags=re.IGNORECASE).strip()
            break
    # Também aceita "ele" no final
    if re.search(r'\bele\b$', texto, re.IGNORECASE):
        pagador = 'Marido'
        texto = re.sub(r'\bele\b$', '', texto, flags=re.IGNORECASE).strip()

    # Extrai valor numérico (ex: 200, 200.00, 200,00, R$200)
    texto = re.sub(r'\bR\$\s*', '', texto)
    match = re.search(r'(\d{1,6}(?:[.,]\d{2})?)', texto)
    if not match:
        return None

    valor_str = match.group().replace(',', '.')
    try:
        valor = float(valor_str)
    except ValueError:
        return None

    if valor <= 0:
        return None

    # Descrição = texto sem o número
    descricao = (texto[:match.start()] + texto[match.end():]).strip()
    descricao = re.sub(r'\s+', ' ', descricao).strip(' -:')
    if not descricao:
        descricao = 'Gasto'

    categoria = categorizar(descricao)
    sugestao = sugerir_categoria(descricao) if not categoria else categoria

    return {
        'pagador':   pagador,
        'descricao': descricao,
        'valor':     valor,
        'categoria': categoria,
        'sugestao':  sugestao,
    }


# ── Planilha: registros ────────────────────────────────────────────────────────
def registrar_gasto(pagador: str, descricao: str, categoria: str, valor: float) -> float:
    sheet = get_sheet()
    ws = sheet.worksheet('Saldo')
    values = ws.get_all_values()

    # Pega o saldo da última linha preenchida
    saldo_atual = 0.0
    for row in values[1:]:
        if len(row) >= 5 and row[4]:
            try:
                saldo_atual = float(str(row[4]).replace(',', '.').replace('R$', '').strip())
            except ValueError:
                pass

    novo_saldo = saldo_atual + valor if 'marido' not in pagador.lower() else saldo_atual - valor

    data = datetime.now().strftime('%d/%m/%Y')
    ws.append_row([data, pagador, descricao, categoria, f'{novo_saldo:.2f}', f'{valor:.2f}'])
    return novo_saldo


def registrar_lancamentos_cartao(lancamentos: list[dict]) -> None:
    sheet = get_sheet()
    ws = sheet.worksheet('Cartão')
    for l in lancamentos:
        ws.append_row([l['data'], l['estabelecimento'], l['categoria'], f"{l['valor']:.2f}"])


# ── PDF ────────────────────────────────────────────────────────────────────────
def processar_pdf(caminho_pdf: str) -> list[dict]:
    lancamentos = []
    seen = set()

    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            texto_completo = ''
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if not row:
                            continue
                        for i, cell in enumerate(row):
                            if cell and re.match(r'\d{2}/\d{2}', str(cell)):
                                try:
                                    data = str(row[i]).strip()
                                    desc = str(row[i + 1]).strip() if i + 1 < len(row) else ''
                                    val_str = str(row[-1]).strip()
                                    val_str = re.sub(r'[R$\s\.]', '', val_str).replace(',', '.')
                                    valor = float(val_str)
                                    desc = re.sub(r'\s*R\$\s*', '', desc).strip()
                                    key = f'{data}_{desc}_{valor}'
                                    if desc and valor > 0 and key not in seen:
                                        seen.add(key)
                                        lancamentos.append({
                                            'data': data,
                                            'estabelecimento': desc,
                                            'categoria': sugerir_categoria(desc),
                                            'valor': valor,
                                        })
                                except Exception:
                                    continue
                texto_completo += page.extract_text() or ''

            # Fallback: regex no texto
            if not lancamentos and texto_completo:
                for data, desc, val in re.findall(
                    r'(\d{2}/\d{2})\s+(.+?)\s+([\d]{1,6}[.,]\d{2})', texto_completo
                ):
                    desc = re.sub(r'\s*R\$\s*', '', desc).strip()
                    val_str = val.replace(',', '.')
                    key = f'{data}_{desc}_{val_str}'
                    if key not in seen:
                        seen.add(key)
                        try:
                            valor = float(val_str)
                            lancamentos.append({
                                'data': f'{data}/{datetime.now().year}',
                                'estabelecimento': desc,
                                'categoria': sugerir_categoria(desc),
                                'valor': valor,
                            })
                        except ValueError:
                            continue
    except Exception as e:
        logger.error(f'Erro ao processar PDF: {e}')

    return lancamentos


# ── Lembretes de contas ────────────────────────────────────────────────────────
def carregar_contas() -> list[dict]:
    p = Path('contas.json')
    if p.exists():
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    return []


async def verificar_lembretes(app: Application) -> None:
    try:
        import holidays
        br_holidays = holidays.Brazil()
    except ImportError:
        br_holidays = {}

    hoje = date.today()
    contas = carregar_contas()

    if 'contas_pagas' not in app.bot_data:
        app.bot_data['contas_pagas'] = set()

    for conta in contas:
        nome     = conta.get('nome', '')
        dia      = conta.get('dia', 1)
        valor    = conta.get('valor', 0)
        chat_id  = conta.get('chat_id')
        if not chat_id:
            continue

        try:
            vencimento = date(hoje.year, hoje.month, dia)
        except ValueError:
            continue

        # Ajusta para dia útil anterior (fim de semana / feriado)
        ajustado = vencimento
        while ajustado.weekday() >= 5 or ajustado in br_holidays:
            ajustado -= timedelta(days=1)

        conta_id = f'{nome}_{hoje.month}_{hoje.year}'
        dias_para_vencer = (ajustado - hoje).days

        if conta_id in app.bot_data['contas_pagas']:
            continue

        if dias_para_vencer not in (3, 1):
            continue

        keyboard = [[
            InlineKeyboardButton('✅ Pago',            callback_data=f'conta:{conta_id}:pago'),
            InlineKeyboardButton('⏰ Ainda pendente', callback_data=f'conta:{conta_id}:pendente'),
        ]]
        valor_str = f' (R${valor:.2f})' if valor else ''
        if dias_para_vencer == 3:
            texto = (f'🔔 Lembrete: *{nome}*{valor_str} vence em 3 dias '
                     f'({ajustado.strftime("%d/%m")}).')
        else:
            texto = (f'⚠️ ATENÇÃO: *{nome}*{valor_str} vence *AMANHÃ* '
                     f'({ajustado.strftime("%d/%m")})!')

        await app.bot.send_message(
            chat_id=chat_id,
            text=texto,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )


# ── Handlers ───────────────────────────────────────────────────────────────────
pending: dict[int, dict] = {}   # msg_id → gasto pendente de categoria


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        '👋 Olá! Sou o bot das *Finanças da Casa*!\n\n'
        '📝 *Como registrar gastos:*\n'
        '• `restaurante 200` — você pagou R$200\n'
        '• `marido padaria 25` — seu marido pagou R$25\n'
        '• `mercado 350 ele` — marido pagou R$350\n\n'
        '📄 *Fatura do cartão:*\n'
        'Envie o PDF da fatura aqui.\n\n'
        '📊 Comandos:\n'
        '/saldo — ver saldo entre vocês\n'
        '/resumo — gastos por categoria este mês\n'
        '/ajuda — instruções completas',
        parse_mode='Markdown',
    )


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        sheet = get_sheet()
        ws    = sheet.worksheet('Saldo')
        rows  = ws.get_all_values()

        saldo = 0.0
        for row in rows[1:]:
            if len(row) >= 5 and row[4]:
                try:
                    saldo = float(str(row[4]).replace(',', '.').replace('R$', '').strip())
                except ValueError:
                    pass

        if saldo > 0:
            msg = f'💰 Saldo atual: *R${saldo:.2f}*\n📊 Você pagou mais este mês.'
        elif saldo < 0:
            msg = f'💰 Saldo atual: *R${abs(saldo):.2f}*\n📊 Seu marido pagou mais este mês.'
        else:
            msg = '💰 Saldo: *R$0,00* ✅ Vocês estão quites!'

        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f'❌ Erro ao buscar saldo: {e}')


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        sheet  = get_sheet()
        ws_s   = sheet.worksheet('Saldo')
        ws_c   = sheet.worksheet('Cartão')
        mes    = datetime.now().month
        ano    = datetime.now().year

        cats: dict[str, float] = {}

        def somar(rows, col_data=0, col_cat=3, col_val=5):
            for row in rows[1:]:
                if len(row) <= max(col_data, col_cat, col_val):
                    continue
                try:
                    d = datetime.strptime(row[col_data], '%d/%m/%Y')
                    if d.month == mes and d.year == ano:
                        cat = row[col_cat] or 'Outros'
                        val = float(str(row[col_val]).replace(',', '.').replace('R$', '').strip())
                        cats[cat] = cats.get(cat, 0) + val
                except Exception:
                    pass

        somar(ws_s.get_all_values(), col_val=5)
        # Cartão: Data|Estabelecimento|Categoria|Valor
        for row in ws_c.get_all_values()[1:]:
            if len(row) >= 4:
                try:
                    d = datetime.strptime(row[0], '%d/%m/%Y')
                    if d.month == mes and d.year == ano:
                        cat = row[2] or 'Outros'
                        val = float(str(row[3]).replace(',', '.'))
                        cats[cat] = cats.get(cat, 0) + val
                except Exception:
                    pass

        if not cats:
            await update.message.reply_text('Nenhum gasto registrado este mês.')
            return

        total = sum(cats.values())
        linhas = [f'📊 *Resumo {datetime.now().strftime("%B/%Y")}*\n']
        for cat, val in sorted(cats.items(), key=lambda x: x[1], reverse=True):
            linhas.append(f'• {cat}: R${val:.2f}')
        linhas.append(f'\n💰 *Total: R${total:.2f}*')

        await update.message.reply_text('\n'.join(linhas), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f'❌ Erro ao gerar resumo: {e}')


async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        '📖 *Instruções completas*\n\n'
        '*Registrar gasto (PIX / dinheiro):*\n'
        '`mercado 200` → você pagou R$200\n'
        '`marido gasolina 150` → marido pagou R$150\n'
        '`restaurante 85,50 ele` → marido pagou R$85,50\n\n'
        '*Fatura do cartão:*\n'
        'Envie o arquivo PDF diretamente aqui.\n\n'
        '*Comandos:*\n'
        '/saldo — saldo entre vocês\n'
        '/resumo — gastos por categoria\n'
        '/start — boas-vindas\n'
        '/ajuda — esta mensagem',
        parse_mode='Markdown',
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    texto = update.message.text.strip()
    if texto.startswith('/'):
        return

    uid = update.message.from_user.id
    if uid == MARIANA_ID:
        nome = 'Mariana'
    elif uid == MARIDO_ID:
        nome = 'Marido'
    else:
        nome = update.message.from_user.first_name or 'Usuário'

    gasto = parse_gasto(texto, nome)
    if not gasto:
        return  # mensagem não é um gasto — ignorar

    if gasto['categoria']:
        _confirmar_e_salvar(update, context, gasto)
        return

    # Categoria desconhecida → pede confirmação
    mid = update.message.message_id
    pending[mid] = gasto
    sugestao = gasto['sugestao']

    teclado = [[InlineKeyboardButton(f'✓ {sugestao}', callback_data=f'cat:{mid}:{sugestao}')]]
    outras = [c for c in CATEGORIAS if c != sugestao]
    for i in range(0, len(outras), 2):
        linha = [InlineKeyboardButton(outras[i], callback_data=f'cat:{mid}:{outras[i]}')]
        if i + 1 < len(outras):
            linha.append(InlineKeyboardButton(outras[i + 1], callback_data=f'cat:{mid}:{outras[i + 1]}'))
        teclado.append(linha)

    await update.message.reply_text(
        f'🤔 Não reconheci *{gasto["descricao"]}* (R${gasto["valor"]:.2f}).\n'
        f'Seria *{sugestao}*?',
        reply_markup=InlineKeyboardMarkup(teclado),
        parse_mode='Markdown',
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith('cat:'):
        _, mid_str, categoria = data.split(':', 2)
        mid = int(mid_str)
        gasto = pending.pop(mid, None)
        if not gasto:
            await query.edit_message_text('⚠️ Gasto não encontrado. Tente novamente.')
            return
        gasto['categoria'] = categoria
        saldo = registrar_gasto(gasto['pagador'], gasto['descricao'], categoria, gasto['valor'])
        await query.edit_message_text(_msg_confirmacao(gasto, saldo), parse_mode='Markdown')

    elif data.startswith('conta:'):
        _, conta_id, resposta = data.split(':', 2)
        if resposta == 'pago':
            if 'contas_pagas' not in context.bot_data:
                context.bot_data['contas_pagas'] = set()
            context.bot_data['contas_pagas'].add(conta_id)
            await query.edit_message_text('✅ Ótimo! Conta marcada como paga.')
        else:
            await query.edit_message_text('⏰ Ok! Vou lembrar novamente 1 dia antes do vencimento.')


def _confirmar_e_salvar(update, context, gasto):
    """Salva gasto já categorizado e responde."""
    import asyncio
    async def _inner():
        saldo = registrar_gasto(gasto['pagador'], gasto['descricao'], gasto['categoria'], gasto['valor'])
        await update.message.reply_text(_msg_confirmacao(gasto, saldo), parse_mode='Markdown')
    asyncio.ensure_future(_inner())


def _msg_confirmacao(gasto: dict, saldo: float) -> str:
    saldo_str = f'+R${saldo:.2f}' if saldo >= 0 else f'-R${abs(saldo):.2f}'
    emoji = '✅'
    return (
        f'{emoji} Registrado!\n'
        f'👤 *{gasto["pagador"]}* pagou R${gasto["valor"]:.2f} em *{gasto["categoria"]}*\n'
        f'📊 Saldo: {saldo_str}'
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith('.pdf'):
        return

    await update.message.reply_text('📄 Recebi o PDF! Processando a fatura...')

    try:
        arquivo   = await context.bot.get_file(doc.file_id)
        pdf_path  = f'/tmp/fatura_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
        await arquivo.download_to_drive(pdf_path)

        lancamentos = processar_pdf(pdf_path)
        Path(pdf_path).unlink(missing_ok=True)

        if not lancamentos:
            await update.message.reply_text(
                '❌ Não consegui extrair transações. Verifique se é uma fatura válida.'
            )
            return

        registrar_lancamentos_cartao(lancamentos)

        total = sum(l['valor'] for l in lancamentos)
        cats: dict[str, float] = {}
        for l in lancamentos:
            cats[l['categoria']] = cats.get(l['categoria'], 0) + l['valor']

        linhas = [f'✅ *Fatura processada!* {len(lancamentos)} transações — Total: R${total:.2f}\n',
                  '📊 Por categoria:']
        for cat, val in sorted(cats.items(), key=lambda x: x[1], reverse=True):
            linhas.append(f'  • {cat}: R${val:.2f}')

        await update.message.reply_text('\n'.join(linhas), parse_mode='Markdown')

    except Exception as e:
        logger.error(f'Erro ao processar PDF: {e}')
        await update.message.reply_text(f'❌ Erro ao processar PDF: {e}')


# ── Inicialização ──────────────────────────────────────────────────────────────
async def post_init(app: Application) -> None:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(verificar_lembretes, 'cron', hour=9, minute=0, args=[app])
    scheduler.start()
    logger.info('Scheduler de lembretes iniciado.')


def main() -> None:
    import asyncio

    if not TOKEN:
        raise ValueError('TELEGRAM_TOKEN não definido no .env')

    # Compatível com Python 3.12+
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler('start',  cmd_start))
    app.add_handler(CommandHandler('saldo',  cmd_saldo))
    app.add_handler(CommandHandler('resumo', cmd_resumo))
    app.add_handler(CommandHandler('ajuda',  cmd_ajuda))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info('Bot iniciado! Aguardando mensagens...')
    app.run_polling()


if __name__ == '__main__':
    main()
