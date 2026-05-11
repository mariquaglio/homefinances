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
PDF_PASSWORD        = os.getenv('PDF_PASSWORD', '')

# Se estiver na nuvem, recria o credentials.json a partir da variável de ambiente
_creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')
if _creds_json_str and not Path(CREDENTIALS_FILE).exists():
    Path(CREDENTIALS_FILE).write_text(_creds_json_str, encoding='utf-8')

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

def fmt_saldo(valor: float) -> str:
    """Formata saldo sem decimais, arredondando 0.5 pra cima. Ex: R$ 500 (+ Mari)"""
    import math
    arredondado = math.floor(abs(valor) + 0.5)
    if valor > 0:
        return f'R$ {arredondado} (+ Mari)'
    elif valor < 0:
        return f'R$ {arredondado} (- Guila)'
    else:
        return 'R$ 0 ✅ Quites!'


def _pagou_marido(pagador: str) -> bool:
    """Retorna True se o pagador for o marido (Guila/Guilherme/Marido)."""
    return any(kw in pagador.lower() for kw in ['marido', 'guila', 'guilherme'])


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

    novo_saldo = saldo_atual - valor if _pagou_marido(pagador) else saldo_atual + valor

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
        kwargs = {'password': PDF_PASSWORD} if PDF_PASSWORD else {}
        with pdfplumber.open(caminho_pdf, **kwargs) as pdf:
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

        msg = f'💰 Saldo: *{fmt_saldo(saldo)}*'

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
        '📖 *Guia de Comandos — Finanças da Casa*\n\n'

        '💸 *Registrar gastos:*\n'
        '`mercado 200` → Mari pagou R$200\n'
        '`marido gasolina 150` → Guila pagou R$150\n'
        '`restaurante 85,50 ele` → Guila pagou R$85,50\n'
        '_Envie o PDF da fatura → cartão processado automaticamente_\n\n'

        '💰 *Saldo:*\n'
        '/saldo — saldo atual entre Mari e Guila\n'
        '/ajustar 5393 — define saldo inicial (use número negativo para Guila)\n\n'

        '📊 *Relatórios:*\n'
        '/resumo — gastos por categoria este mês\n'
        '/dashboard — gráfico do mês + insights\n'
        '/grafico — fatura do cartão mês a mês 💳\n'
        '/gastos — gasto total (PIX + cartão) mês a mês 📈\n\n'

        '📅 *Contas fixas:*\n'
        '/proxima — próximas contas a vencer\n\n'

        '✏️ *Corrigir lançamentos:*\n'
        '/deletar — apagar um lançamento\n'
        '/corrigir — corrigir um lançamento\n\n'

        '❓ *Outros:*\n'
        '/start — boas-vindas\n'
        '/ajuda — este menu',
        parse_mode='Markdown',
    )


def _ultimos_lancamentos(ws, n=5) -> list[dict]:
    """Retorna os últimos n lançamentos da aba Saldo."""
    rows = ws.get_all_values()
    dados = [(i + 2, row) for i, row in enumerate(rows[1:]) if any(row)]
    return [{'linha': r, 'data': row[0], 'pagador': row[1], 'desc': row[2],
             'cat': row[3], 'saldo': row[4], 'valor': row[5]}
            for r, row in dados[-n:]]


async def cmd_ajustar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /ajustar 5393   → define saldo inicial como +R$5.393,00 (Mariana pagou mais)
    /ajustar -2000  → define saldo inicial como -R$2.000,00 (Marido pagou mais)
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            '💡 *Como usar:*\n'
            '`/ajustar 5393` → saldo inicial +R$5.393 (Mari pagou mais)\n'
            '`/ajustar -2000` → saldo inicial -R$2.000 (Guila pagou mais)',
            parse_mode='Markdown',
        )
        return

    try:
        valor_str = args[0].replace(',', '.').replace('R$', '').strip()
        saldo_inicial = float(valor_str)
    except ValueError:
        await update.message.reply_text('❌ Valor inválido. Ex: `/ajustar 5393` ou `/ajustar -2000`',
                                        parse_mode='Markdown')
        return

    try:
        sheet = get_sheet()
        ws = sheet.worksheet('Saldo')
        data_hoje = datetime.now().strftime('%d/%m/%Y')

        # Insere linha de saldo inicial logo após o cabeçalho (linha 2)
        ws.insert_row(
            [data_hoje, 'Saldo Inicial', 'Saldo Inicial', 'Ajuste',
             f'{saldo_inicial:.2f}', f'{abs(saldo_inicial):.2f}'],
            index=2
        )

        # Recalcula todos os saldos a partir do saldo inicial
        rows = ws.get_all_values()
        saldo = saldo_inicial
        for i, row in enumerate(rows[2:], start=3):   # pula cabeçalho + linha inicial
            if len(row) >= 6 and row[5]:
                pagador_row = row[1]
                if pagador_row == 'Saldo Inicial':
                    continue
                try:
                    val = float(str(row[5]).replace(',', '.'))
                    saldo = saldo + val if 'marido' not in pagador_row.lower() else saldo - val
                    ws.update_cell(i, 5, f'{saldo:.2f}')
                except Exception:
                    pass

        await update.message.reply_text(
            f'✅ Saldo inicial definido!\n'
            f'💰 *{fmt_saldo(saldo_inicial)}*\n\n'
            f'Todos os lançamentos futuros serão calculados a partir deste valor.',
            parse_mode='Markdown',
        )
    except Exception as e:
        await update.message.reply_text(f'❌ Erro ao ajustar saldo: {e}')


async def cmd_deletar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        sheet = get_sheet()
        ws = sheet.worksheet('Saldo')
        ultimos = _ultimos_lancamentos(ws)

        if not ultimos:
            await update.message.reply_text('Nenhum lançamento encontrado.')
            return

        teclado = []
        for l in reversed(ultimos):
            data_curta = l['data'][:5]  # DD/MM
            desc_curta = l['desc'][:18].strip()
            pagador_curto = 'Mari' if 'mariana' in l['pagador'].lower() else 'Marido'
            try:
                valor_fmt = f"R${float(l['valor']):.0f}"
            except Exception:
                valor_fmt = f"R${l['valor']}"
            label = f"❌ {data_curta} {pagador_curto} {desc_curta} {valor_fmt}"
            teclado.append([InlineKeyboardButton(label, callback_data=f"del:{l['linha']}")])
        teclado.append([InlineKeyboardButton('🚫 Cancelar', callback_data='del:cancel')])

        await update.message.reply_text(
            '🗑 *Qual lançamento deseja deletar?*',
            reply_markup=InlineKeyboardMarkup(teclado),
            parse_mode='Markdown',
        )
    except Exception as e:
        await update.message.reply_text(f'❌ Erro: {e}')


async def cmd_corrigir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        sheet = get_sheet()
        ws = sheet.worksheet('Saldo')
        ultimos = _ultimos_lancamentos(ws)

        if not ultimos:
            await update.message.reply_text('Nenhum lançamento encontrado.')
            return

        teclado = []
        for l in reversed(ultimos):
            data_curta = l['data'][:5]  # DD/MM
            desc_curta = l['desc'][:18].strip()
            pagador_curto = 'Mari' if 'mariana' in l['pagador'].lower() else 'Marido'
            try:
                valor_fmt = f"R${float(l['valor']):.0f}"
            except Exception:
                valor_fmt = f"R${l['valor']}"
            label = f"✏️ {data_curta} {pagador_curto} {desc_curta} {valor_fmt}"
            teclado.append([InlineKeyboardButton(label, callback_data=f"corr:{l['linha']}")])
        teclado.append([InlineKeyboardButton('🚫 Cancelar', callback_data='corr:cancel')])

        await update.message.reply_text(
            '✏️ *Qual lançamento deseja corrigir?*',
            reply_markup=InlineKeyboardMarkup(teclado),
            parse_mode='Markdown',
        )
    except Exception as e:
        await update.message.reply_text(f'❌ Erro: {e}')


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

    # ── Modo correção: aguardando texto do usuário após /corrigir ──────────────
    linha_corrigir = context.user_data.get('corrigir_linha')
    if linha_corrigir:
        gasto = parse_gasto(texto, nome)
        if not gasto:
            await update.message.reply_text(
                '❌ Não entendi. Use o formato: `descrição valor`\n'
                'Exemplo: `mercado 250`\n\n'
                'Ou envie /corrigir para escolher de novo.',
                parse_mode='Markdown',
            )
            return

        categoria = gasto['categoria'] or gasto['sugestao']
        try:
            sheet = get_sheet()
            ws = sheet.worksheet('Saldo')
            data_hoje = datetime.now().strftime('%d/%m/%Y')
            # Atualiza as colunas: Data, Pagador, Descrição, Categoria, (Saldo recalculado depois), Valor
            ws.update(f'A{linha_corrigir}:D{linha_corrigir}',
                      [[data_hoje, gasto['pagador'], gasto['descricao'], categoria]])
            ws.update_cell(linha_corrigir, 6, f'{gasto["valor"]:.2f}')

            # Recalcula saldos acumulados a partir da linha corrigida
            rows = ws.get_all_values()
            saldo = 0.0
            for i, row in enumerate(rows[1:], start=2):
                if len(row) >= 6 and row[5]:
                    pagador_row = row[1]
                    try:
                        val = float(str(row[5]).replace(',', '.'))
                        saldo = saldo - val if _pagou_marido(pagador_row) else saldo + val
                        ws.update_cell(i, 5, f'{saldo:.2f}')
                    except Exception:
                        pass

            context.user_data.pop('corrigir_linha', None)
            await update.message.reply_text(
                f'✅ Lançamento corrigido!\n'
                f'👤 *{gasto["pagador"]}* pagou R${gasto["valor"]:.2f} em *{categoria}*\n'
                f'📊 Saldo: {fmt_saldo(saldo)}',
                parse_mode='Markdown',
            )
        except Exception as e:
            await update.message.reply_text(f'❌ Erro ao corrigir: {e}')
        return

    gasto = parse_gasto(texto, nome)
    if not gasto:
        return  # mensagem não é um gasto — ignorar

    if gasto['categoria']:
        await _confirmar_e_salvar(update, context, gasto)
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

    elif data.startswith('del:'):
        _, linha_str = data.split(':', 1)
        if linha_str == 'cancel':
            await query.edit_message_text('Cancelado.')
            return
        try:
            sheet = get_sheet()
            ws = sheet.worksheet('Saldo')
            linha = int(linha_str)
            ws.delete_rows(linha)
            # Recalcula saldos acumulados
            rows = ws.get_all_values()
            saldo = 0.0
            for i, row in enumerate(rows[1:], start=2):
                if len(row) >= 6 and row[5]:
                    pagador = row[1]
                    try:
                        valor = float(str(row[5]).replace(',', '.'))
                        saldo = saldo - valor if _pagou_marido(pagador) else saldo + valor
                        ws.update_cell(i, 5, f'{saldo:.2f}')
                    except Exception:
                        pass
            await query.edit_message_text('🗑 Lançamento deletado e saldo recalculado!')
        except Exception as e:
            await query.edit_message_text(f'❌ Erro ao deletar: {e}')

    elif data.startswith('graf:') or data.startswith('gast:'):
        tipo, ano_mes = data.split(':', 1)
        if ano_mes == 'noop':
            return
        try:
            ano, mes = map(int, ano_mes.split('-'))
            sheet = get_sheet()
            from telegram import InputMediaPhoto
            if tipo == 'graf':
                buf = await _gerar_buf_grafico(sheet, ano, mes)
                caption = '💳 Fatura do cartão — navegue pelos meses'
            else:
                buf = await _gerar_buf_gastos(sheet, ano, mes)
                caption = '📊 Gasto total (PIX + cartão) — navegue pelos meses'
            await query.edit_message_media(
                media=InputMediaPhoto(media=buf, caption=caption),
                reply_markup=_teclado_grafico(ano, mes, tipo),
            )
        except Exception as e:
            await query.answer(f'Erro: {e}', show_alert=True)

    elif data.startswith('corr:'):
        _, linha_str = data.split(':', 1)
        if linha_str == 'cancel':
            await query.edit_message_text('Cancelado.')
            return
        context.user_data['corrigir_linha'] = int(linha_str)
        await query.edit_message_text(
            '✏️ Digite a correção no formato:\n'
            '`descrição valor`\n\n'
            'Exemplo: `mercado 250`',
            parse_mode='Markdown',
        )


def _gastos_por_mes(ws_s, ws_c, mes: int, ano: int) -> dict[str, float]:
    """Soma gastos por categoria para um dado mês/ano (PIX + Cartão)."""
    cats: dict[str, float] = {}
    IGNORAR = {'Saldo Inicial', 'Ajuste', 'Cartão'}

    for row in ws_s.get_all_values()[1:]:
        if len(row) < 6 or not row[0] or row[1] == 'Saldo Inicial':
            continue
        try:
            d = datetime.strptime(row[0], '%d/%m/%Y')
            if d.month == mes and d.year == ano:
                cat = row[3] or 'Outros'
                if cat in IGNORAR:
                    continue
                val = float(str(row[5]).replace(',', '.').replace('R$', '').strip())
                cats[cat] = cats.get(cat, 0) + val
        except Exception:
            pass

    for row in ws_c.get_all_values()[1:]:
        if len(row) < 4:
            continue
        try:
            d = datetime.strptime(row[0], '%d/%m/%Y')
            if d.month == mes and d.year == ano:
                cat = row[2] or 'Outros'
                val = float(str(row[3]).replace(',', '.'))
                cats[cat] = cats.get(cat, 0) + val
        except Exception:
            pass

    return cats


def _gerar_insights(atual: dict, anterior: dict, media3: dict) -> str:
    """Gera texto de insights comparando o mês atual com o anterior e média 3 meses."""
    linhas = ['💡 *Insights do mês*\n']
    total_atual   = sum(atual.values())
    total_ant     = sum(anterior.values())
    total_media3  = sum(media3.values())

    # Comparação total
    if total_ant > 0:
        diff_ant = total_atual - total_ant
        pct_ant  = diff_ant / total_ant * 100
        sinal    = '▲' if diff_ant > 0 else '▼'
        linhas.append(f'*vs mês anterior:* {sinal} R${abs(diff_ant):.0f} ({abs(pct_ant):.0f}%)')
    if total_media3 > 0:
        diff_m3 = total_atual - total_media3
        pct_m3  = diff_m3 / total_media3 * 100
        sinal   = '▲' if diff_m3 > 0 else '▼'
        linhas.append(f'*vs média 3 meses:* {sinal} R${abs(diff_m3):.0f} ({abs(pct_m3):.0f}%)\n')

    # Categorias que subiram (vs mês anterior)
    subiram  = []
    caíram   = []
    todas_cats = set(atual) | set(anterior)
    for cat in todas_cats:
        v_atual = atual.get(cat, 0)
        v_ant   = anterior.get(cat, 0)
        if v_ant == 0 and v_atual > 0:
            subiram.append((cat, v_atual, None))
        elif v_ant > 0 and v_atual > v_ant * 1.15:   # subiu mais de 15%
            subiram.append((cat, v_atual, (v_atual - v_ant) / v_ant * 100))
        elif v_ant > 0 and v_atual < v_ant * 0.85:   # caiu mais de 15%
            caíram.append((cat, v_atual, (v_ant - v_atual) / v_ant * 100))

    subiram.sort(key=lambda x: x[1], reverse=True)
    caíram.sort(key=lambda x: x[2], reverse=True)

    if subiram:
        linhas.append('⚠️ *Atenção — aumentou vs mês passado:*')
        for cat, val, pct in subiram[:3]:
            pct_str = f' (+{pct:.0f}%)' if pct else ' (novo)'
            linhas.append(f'  • {cat}: R${val:.0f}{pct_str}')
        linhas.append('')

    if caíram:
        linhas.append('✅ *Reduções — parabéns:*')
        for cat, val, pct in caíram[:3]:
            linhas.append(f'  • {cat}: R${val:.0f} (-{pct:.0f}%)')
        linhas.append('')

    # Top 3 categorias para cortar (maiores gastos vs média 3 meses)
    oportunidades = []
    for cat in atual:
        v_atual = atual[cat]
        v_media = media3.get(cat, 0)
        if v_media > 0 and v_atual > v_media * 1.10:
            oportunidades.append((cat, v_atual, v_atual - v_media))
    oportunidades.sort(key=lambda x: x[2], reverse=True)

    if oportunidades:
        linhas.append('✂️ *Onde reduzir (acima da sua média):*')
        for cat, val, exc in oportunidades[:3]:
            linhas.append(f'  • {cat}: R${val:.0f} (R${exc:.0f} acima do normal)')

    return '\n'.join(linhas)


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gera gráfico de gastos do mês por categoria (PIX + Cartão) + insights."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import io
    except ImportError:
        await update.message.reply_text('❌ Instale matplotlib: `pip install matplotlib`',
                                        parse_mode='Markdown')
        return

    try:
        sheet    = get_sheet()
        ws_s     = sheet.worksheet('Saldo')
        ws_c     = sheet.worksheet('Cartão')
        hoje     = datetime.now()
        mes, ano = hoje.month, hoje.year
        nome_mes = hoje.strftime('%B/%Y').capitalize()

        # ── Dados do mês atual e períodos anteriores ───────────────────────────
        cats = _gastos_por_mes(ws_s, ws_c, mes, ano)

        mes_ant = (hoje.replace(day=1) - timedelta(days=1))
        anterior = _gastos_por_mes(ws_s, ws_c, mes_ant.month, mes_ant.year)

        media3: dict[str, float] = {}
        contagem3: dict[str, int] = {}
        for delta in range(1, 4):
            ref = hoje.replace(day=1) - timedelta(days=delta * 28)
            dados = _gastos_por_mes(ws_s, ws_c, ref.month, ref.year)
            for cat, val in dados.items():
                media3[cat]    = media3.get(cat, 0) + val
                contagem3[cat] = contagem3.get(cat, 0) + 1
        for cat in media3:
            media3[cat] /= contagem3[cat]

        if not cats:
            await update.message.reply_text(f'Nenhum gasto registrado em {nome_mes}.')
            return

        # ── Gráfico ────────────────────────────────────────────────────────────
        cats_sorted = sorted(cats.items(), key=lambda x: x[1], reverse=True)
        labels  = [c for c, _ in cats_sorted]
        valores = [v for _, v in cats_sorted]
        total   = sum(valores)

        CORES = ['#4C72B0','#DD8452','#55A868','#C44E52','#8172B2',
                 '#937860','#DA8BC3','#8C8C8C','#CCB974','#64B5CD',
                 '#E377C2','#7F7F7F','#BCBD22','#17BECF']

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 6))
        fig.patch.set_facecolor('#F8F9FA')

        wedges, texts, autotexts = ax1.pie(
            valores, labels=labels, autopct='%1.0f%%',
            colors=CORES[:len(labels)], startangle=140,
            pctdistance=0.82, textprops={'fontsize': 9}
        )
        for at in autotexts:
            at.set_fontsize(8)
        ax1.set_title(f'Gastos por Categoria\n{nome_mes}', fontsize=12, fontweight='bold', pad=15)

        bars = ax2.barh(labels[::-1], valores[::-1], color=CORES[:len(labels)][::-1], height=0.6)
        for bar, val in zip(bars, valores[::-1]):
            ax2.text(bar.get_width() + total * 0.01, bar.get_y() + bar.get_height() / 2,
                     f'R${val:.0f}', va='center', fontsize=9)
        ax2.set_xlabel('R$', fontsize=10)
        ax2.set_title(f'Valor por Categoria\nTotal: R${total:.0f}', fontsize=12, fontweight='bold')
        ax2.set_facecolor('#F8F9FA')
        ax2.spines['top'].set_visible(False)
        ax2.spines['right'].set_visible(False)
        ax2.set_xlim(0, max(valores) * 1.2)
        plt.tight_layout(pad=2)

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)

        caption = f'📊 *Dashboard {nome_mes}*\n💰 Total: R${total:.0f}'
        await update.message.reply_photo(photo=buf, caption=caption, parse_mode='Markdown')

        # ── Insights ───────────────────────────────────────────────────────────
        insights = _gerar_insights(cats, anterior, media3)
        await update.message.reply_text(insights, parse_mode='Markdown')

    except Exception as e:
        logger.error(f'Erro no dashboard: {e}')
        await update.message.reply_text(f'❌ Erro ao gerar dashboard: {e}')


async def cmd_proxima(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra as próximas contas a vencer."""
    try:
        import holidays as hol
        br_holidays = hol.Brazil()
    except ImportError:
        br_holidays = {}

    hoje = date.today()
    contas = carregar_contas()
    proximas = []

    for conta in contas:
        nome  = conta.get('nome', '')
        dia   = conta.get('dia', 1)
        valor = conta.get('valor', 0)

        for delta_mes in range(2):  # este mês e o próximo
            mes_ref = (hoje.month - 1 + delta_mes) % 12 + 1
            ano_ref = hoje.year + ((hoje.month - 1 + delta_mes) // 12)
            try:
                venc = date(ano_ref, mes_ref, dia)
            except ValueError:
                continue

            ajustado = venc
            while ajustado.weekday() >= 5 or ajustado in br_holidays:
                ajustado -= timedelta(days=1)

            dias = (ajustado - hoje).days
            if dias >= 0:
                proximas.append((dias, nome, valor, ajustado))
                break

    if not proximas:
        await update.message.reply_text('Nenhuma conta encontrada.')
        return

    proximas.sort(key=lambda x: x[0])
    linhas = ['📅 *Próximas contas:*\n']
    for dias, nome, valor, venc in proximas[:5]:
        if dias == 0:
            quando = '🔴 *HOJE*'
        elif dias == 1:
            quando = '🟠 *AMANHÃ*'
        elif dias <= 5:
            quando = f'🟡 em {dias} dias ({venc.strftime("%d/%m")})'
        else:
            quando = f'🟢 em {dias} dias ({venc.strftime("%d/%m")})'
        linhas.append(f'{quando} — {nome}: R${valor:,.0f}'.replace(',', '.'))

    await update.message.reply_text('\n'.join(linhas), parse_mode='Markdown')


def _meses_range(ano: int, mes: int, antes=4, depois=2):
    """Retorna lista de (ano, mes) centrada no mês dado."""
    resultado = []
    for delta in range(-antes, depois + 1):
        m, a = mes + delta, ano
        while m < 1:  m += 12; a -= 1
        while m > 12: m -= 12; a += 1
        resultado.append((a, m))
    return resultado


def _buf_barras(labels, valores, cores, titulo, cor_valor='white'):
    """Gera PNG de barras horizontais simples."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import io

    fig, ax = plt.subplots(figsize=(13, 5))
    fig.patch.set_facecolor('#1a1a2e')
    ax.set_facecolor('#1a1a2e')

    bars = ax.bar(range(len(labels)), valores, color=cores, alpha=0.9, width=0.6)
    for bar, val in zip(bars, valores):
        if val > 0:
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(valores, default=1) * 0.01,
                    f'R${val:,.0f}'.replace(',', '.'),
                    ha='center', va='bottom', fontsize=8, color=cor_valor)

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, color='white', fontsize=8)
    ax.yaxis.set_visible(False)
    ax.spines[:].set_visible(False)
    ax.set_title(titulo, color='white', fontsize=13, fontweight='bold', pad=12)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='#1a1a2e')
    buf.seek(0)
    plt.close(fig)
    return buf


async def _gerar_buf_grafico(sheet, ano: int, mes: int):
    """Gráfico só de cartão de crédito, mês a mês."""
    ws_c = sheet.worksheet('Cartão')

    meses = _meses_range(ano, mes)
    gastos = {}
    for row in ws_c.get_all_values()[1:]:
        if len(row) < 4:
            continue
        try:
            d = datetime.strptime(row[0], '%d/%m/%Y')
            val = float(str(row[3]).replace(',', '.'))
            gastos[(d.year, d.month)] = gastos.get((d.year, d.month), 0) + val
        except Exception:
            pass

    nomes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    hoje  = date.today()
    labels, valores, cores = [], [], []
    for a, m in meses:
        labels.append(f'{nomes[m-1]}\n{a}')
        valores.append(gastos.get((a, m), 0))
        if (a, m) == (ano, mes):
            cores.append('#A78BFA')
        elif (a, m) < (hoje.year, hoje.month):
            cores.append('#64B5CD')
        else:
            cores.append('#444466')

    return _buf_barras(labels, valores, cores, f'💳 Fatura do Cartão — {nomes[mes-1]}/{ano}')


async def _gerar_buf_gastos(sheet, ano: int, mes: int):
    """Gráfico de gasto global (PIX + cartão) mês a mês."""
    ws_s = sheet.worksheet('Saldo')
    ws_c = sheet.worksheet('Cartão')
    IGNORAR = {'Saldo Inicial', 'Ajuste'}

    meses = _meses_range(ano, mes)
    gastos: dict = {}

    # PIX (aba Saldo)
    for row in ws_s.get_all_values()[1:]:
        if len(row) < 6 or not row[0] or row[1] in IGNORAR or row[3] in IGNORAR:
            continue
        if row[3] == 'Cartão':   # evita dupla contagem
            continue
        try:
            d = datetime.strptime(row[0], '%d/%m/%Y')
            val = float(str(row[5]).replace(',', '.').replace('R$', '').strip())
            gastos[(d.year, d.month)] = gastos.get((d.year, d.month), 0) + val
        except Exception:
            pass

    # Cartão (aba Cartão)
    for row in ws_c.get_all_values()[1:]:
        if len(row) < 4:
            continue
        try:
            d = datetime.strptime(row[0], '%d/%m/%Y')
            val = float(str(row[3]).replace(',', '.'))
            gastos[(d.year, d.month)] = gastos.get((d.year, d.month), 0) + val
        except Exception:
            pass

    nomes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    hoje  = date.today()
    labels, valores, cores = [], [], []
    for a, m in meses:
        labels.append(f'{nomes[m-1]}\n{a}')
        valores.append(gastos.get((a, m), 0))
        if (a, m) == (ano, mes):
            cores.append('#34D399')
        elif (a, m) < (hoje.year, hoje.month):
            cores.append('#6EE7B7')
        else:
            cores.append('#2D6A4F')

    return _buf_barras(labels, valores, cores, f'📊 Gasto Total (PIX + Cartão) — {nomes[mes-1]}/{ano}', cor_valor='#D1FAE5')

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='#1a1a2e')
    buf.seek(0)
    plt.close(fig)
    return buf


def _teclado_grafico(ano: int, mes: int, tipo: str = 'graf') -> InlineKeyboardMarkup:
    """tipo: 'graf' (cartão) ou 'gast' (global)"""
    ma = mes - 1 if mes > 1 else 12
    aa = ano if mes > 1 else ano - 1
    mp = mes + 1 if mes < 12 else 1
    ap = ano if mes < 12 else ano + 1
    nomes = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez']
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f'◀ {nomes[ma-1]}', callback_data=f'{tipo}:{aa}-{ma:02d}'),
        InlineKeyboardButton(f'{nomes[mes-1]}/{ano}', callback_data=f'{tipo}:noop'),
        InlineKeyboardButton(f'{nomes[mp-1]} ▶', callback_data=f'{tipo}:{ap}-{mp:02d}'),
    ]])


async def cmd_grafico(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gráfico de fatura do cartão mês a mês."""
    hoje = datetime.now()
    try:
        sheet = get_sheet()
        buf = await _gerar_buf_grafico(sheet, hoje.year, hoje.month)
        await update.message.reply_photo(
            photo=buf,
            caption='💳 Fatura do cartão — navegue pelos meses',
            reply_markup=_teclado_grafico(hoje.year, hoje.month, 'graf'),
        )
    except Exception as e:
        await update.message.reply_text(f'❌ Erro ao gerar gráfico: {e}')


async def cmd_gastos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gráfico de gasto total (PIX + cartão) mês a mês."""
    hoje = datetime.now()
    try:
        sheet = get_sheet()
        buf = await _gerar_buf_gastos(sheet, hoje.year, hoje.month)
        await update.message.reply_photo(
            photo=buf,
            caption='📊 Gasto total (PIX + cartão) — navegue pelos meses',
            reply_markup=_teclado_grafico(hoje.year, hoje.month, 'gast'),
        )
    except Exception as e:
        await update.message.reply_text(f'❌ Erro ao gerar gráfico: {e}')


async def _confirmar_e_salvar(update, context, gasto):
    """Salva gasto já categorizado e responde."""
    saldo = registrar_gasto(gasto['pagador'], gasto['descricao'], gasto['categoria'], gasto['valor'])
    await update.message.reply_text(_msg_confirmacao(gasto, saldo), parse_mode='Markdown')


def _msg_confirmacao(gasto: dict, saldo: float) -> str:
    return (
        f'✅ Registrado!\n'
        f'👤 *{gasto["pagador"]}* pagou R${gasto["valor"]:.2f} em *{gasto["categoria"]}*\n'
        f'📊 Saldo: {fmt_saldo(saldo)}'
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

        # Lança o total da fatura no Saldo como pagamento do Guila
        mes_ref = datetime.now().strftime('%m/%Y')
        novo_saldo = registrar_gasto('Guila', f'Fatura Cartão {mes_ref}', 'Cartão', total)

        linhas = [f'✅ *Fatura processada!* {len(lancamentos)} transações\n',
                  f'💳 Total: *R${total:.2f}* (lançado como Guila pagou)',
                  f'📊 Saldo atualizado: *{fmt_saldo(novo_saldo)}*\n',
                  '📂 Por categoria:']
        for cat, val in sorted(cats.items(), key=lambda x: x[1], reverse=True):
            linhas.append(f'  • {cat}: R${val:.2f}')
        linhas.append('\n_Use /dashboard para ver o gráfico do mês._')

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

    app.add_handler(CommandHandler('start',   cmd_start))
    app.add_handler(CommandHandler('saldo',   cmd_saldo))
    app.add_handler(CommandHandler('resumo',  cmd_resumo))
    app.add_handler(CommandHandler('ajuda',   cmd_ajuda))
    app.add_handler(CommandHandler('deletar',   cmd_deletar))
    app.add_handler(CommandHandler('corrigir',  cmd_corrigir))
    app.add_handler(CommandHandler('ajustar',   cmd_ajustar))
    app.add_handler(CommandHandler('dashboard', cmd_dashboard))
    app.add_handler(CommandHandler('proxima',   cmd_proxima))
    app.add_handler(CommandHandler('grafico',   cmd_grafico))
    app.add_handler(CommandHandler('gastos',    cmd_gastos))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info('Bot iniciado! Aguardando mensagens...')
    app.run_polling()


if __name__ == '__main__':
    main()
