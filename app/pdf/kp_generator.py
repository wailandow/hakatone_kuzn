"""Render a Softline quote from validated catalog lines, never model-supplied prices."""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fpdf import FPDF
from fpdf.enums import Align, TableCellFillMode
from fpdf.fonts import FontFace


CENT = Decimal('0.01')
ASSETS_DIR = Path(__file__).with_name('assets')
_LOGO_PATH = ASSETS_DIR / 'logo.jpg'
_STAMP_PATH = ASSETS_DIR / 'stamp.jpg'
_FOOTER_PATH = ASSETS_DIR / 'footer.png'

_COMPANY_CONTACTS = (
    '119021, город Москва, ул Льва Толстого, д.',
    '5 стр. 1, этаж 3, помещ. 1, ком. №2, 2а (а-311)',
    'Т +7 495 232 00 23 | info@softline.com',
    'www.softline.ru | www.softline.com',
)


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class QuoteLine:
    sku: str
    name: str
    quantity: int
    price: Decimal
    registry: bool

    def amounts(self, discount):
        unit = money(self.price * (100 - discount) / 100)
        subtotal = unit * self.quantity
        vat = Decimal('0.00') if self.registry else money(subtotal * 20 / 120)
        return unit, subtotal, vat


class _QuotePDF(FPDF):
    def footer(self):
        if _FOOTER_PATH.is_file():
            footer_w = 133.4
            self.image(str(_FOOTER_PATH), x=(self.w - footer_w) / 2, y=self.h - 24, w=footer_w)


def generate_pdf(path: Path, font_dir: Path, lines: list[QuoteLine], discount: Decimal,
                 payment: str, delivery: str, *, today: date | None = None,
                 customer: str = 'Заказчику') -> Decimal:
    """Publish atomically in the destination directory; paths are backend configuration."""
    issue_date = today or date.today()
    pdf = _QuotePDF(orientation='P', unit='mm', format='A4')
    pdf.set_margins(left=12.5, top=14.0, right=12.5)
    pdf.set_auto_page_break(auto=True, margin=28.0)
    pdf.add_font('DejaVu', fname=font_dir / 'DejaVuSans.ttf')
    pdf.add_font('DejaVu', style='B', fname=font_dir / 'DejaVuSans-Bold.ttf')
    pdf.set_title('Коммерческое предложение — ПАО «Софтлайн»')
    pdf.add_page()

    # Header left: Softline logo and legal/contact block.
    if _LOGO_PATH.is_file():
        pdf.image(str(_LOGO_PATH), x=17.6, y=14.8, w=48.5)
    pdf.set_text_color(70, 70, 70)
    pdf.set_font('DejaVu', size=8.5)
    y_contact = 29.5
    for line_text in _COMPANY_CONTACTS:
        pdf.set_xy(17.1, y_contact)
        pdf.cell(95, 4.8, line_text)
        y_contact += 4.8

    # Header right: recipient and outgoing reference date.
    pdf.set_text_color(0, 0, 0)
    pdf.set_font('DejaVu', size=9.5)
    pdf.set_xy(112, 20.5)
    pdf.cell(85.5, 5, f'Куда: {customer}', align='R')
    pdf.set_xy(112, 29.2)
    pdf.cell(85.5, 5, f'Исх. № бн от {issue_date:%d.%m.%Y}.', align='R')

    # Greeting and intro centered above the table.
    pdf.set_y(66)
    pdf.set_font('DejaVu', 'B', 10.5)
    pdf.cell(0, 6, 'Уважаемый заказчик!', align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)
    pdf.set_font('DejaVu', size=9.5)
    pdf.multi_cell(
        0, 5,
        'ПАО «Софтлайн» предлагает Вашему вниманию коммерческое предложение на следующих условиях:',
        align='C', new_x='LMARGIN', new_y='NEXT',
    )
    pdf.ln(4)

    # Quote table matching templateKP.pdf proportions (total width = 185 mm).
    pdf.set_font('DejaVu', size=8.5)
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.25)
    total = total_vat = Decimal('0.00')
    header_style = FontFace(emphasis='B', size_pt=8.5)
    total_style = FontFace(emphasis='B', size_pt=8.5)
    with pdf.table(
        col_widths=(27, 64, 15, 24, 25, 30),
        text_align=('L', 'L', 'C', 'R', 'R', 'R'),
        headings_style=header_style,
        cell_fill_mode=TableCellFillMode.NONE,
        line_height=4.8,
        padding=1.5,
        width=185,
        align=Align.C,
    ) as table:
        header = table.row()
        for title in ('Артикул', 'Наименование', 'Кол-\nво', 'Цена, руб.', 'Сумма, руб.', 'В т.ч. НДС,\nруб.'):
            header.cell(title, align='C')
        for line in lines:
            unit, subtotal, vat = line.amounts(discount)
            total += subtotal
            total_vat += vat
            tax = f'{vat:.2f}' + ('\nБез НДС (реестр ПО)' if line.registry else '')
            table.row((line.sku, line.name, str(line.quantity), f'{unit:.2f}', f'{subtotal:.2f}', tax))
        total_row = table.row(style=total_style)
        total_row.cell('')
        total_row.cell('Итого', align='C')
        total_row.cell('')
        total_row.cell('')
        total_row.cell(f'{total:.2f}', align='R')
        total_row.cell(f'{total_vat:.2f}', align='R')

    # Commercial terms block indented under the table.
    pdf.ln(5)
    terms_x = 27.5
    terms_w = pdf.w - terms_x - 15.0

    def term_line(text, *, size=8.8, color=(0, 0, 0)):
        pdf.set_text_color(*color)
        pdf.set_font('DejaVu', size=size)
        pdf.set_x(terms_x)
        pdf.multi_cell(terms_w, 4.6, text, align='L', new_x='LMARGIN', new_y='NEXT')

    if discount:
        term_line(f'Скидка {discount.normalize():f}% включена в стоимость.')
    term_line('Цена указана с учетом всех налогов, сборов, и других обязательных платежей.')
    term_line(f'Условия оплаты: {payment.rstrip(".")}.')
    term_line(f'Срок поставки: {delivery.rstrip(".")}.')
    expires = issue_date + timedelta(days=14)
    term_line(f'Срок действия коммерческого предложения - до {expires:%d.%m.%Y}.')
    pdf.ln(2)
    term_line(
        'Примечание: Предварительное КП сформировано ИИ-ассистентом для согласования с менеджером ПАО «Софтлайн».',
        size=7.8, color=(90, 90, 90),
    )

    # Signature and stamp block.
    block_h = 50.0
    if pdf.get_y() + block_h > pdf.h - 30.0:
        pdf.add_page()
    sig_top = max(pdf.get_y() + 10.0, 173.0)
    if sig_top + block_h > pdf.h - 28.0:
        sig_top = pdf.get_y() + 8.0
    pdf.set_text_color(0, 0, 0)
    pdf.set_font('DejaVu', size=9.5)
    pdf.set_xy(21.8, sig_top + 19.5)
    pdf.cell(75, 5, 'Бондаренко Александр')
    pdf.set_xy(21.8, sig_top + 24.5)
    pdf.cell(75, 5, 'Директор департамента')
    if _STAMP_PATH.is_file():
        pdf.image(str(_STAMP_PATH), x=125.5, y=sig_top, w=58.7)

    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix='.kp_', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        pdf.output(str(temporary))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return total
