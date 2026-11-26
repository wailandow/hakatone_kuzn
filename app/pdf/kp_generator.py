"""Render a Softline quote from validated catalog lines, never model-supplied prices."""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fpdf import FPDF
from fpdf.fonts import FontFace


CENT = Decimal('0.01')


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


def generate_pdf(path: Path, font_dir: Path, lines: list[QuoteLine], discount: Decimal,
                 payment: str, delivery: str, *, today: date | None = None) -> Decimal:
    """Publish atomically in the destination directory; paths are backend configuration."""
    pdf = FPDF()
    pdf.add_font('DejaVu', fname=font_dir / 'DejaVuSans.ttf')
    pdf.add_font('DejaVu', style='B', fname=font_dir / 'DejaVuSans-Bold.ttf')
    pdf.set_title('Коммерческое предложение — ПАО «Софтлайн»')
    pdf.add_page()
    pdf.set_font('DejaVu', 'B', 15)
    pdf.cell(0, 10, 'Уважаемый заказчик!', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('DejaVu', size=10)

    def paragraph(text):
        pdf.multi_cell(0, 5, text, align='L', new_x='LMARGIN', new_y='NEXT')

    paragraph('ПАО «Софтлайн» предлагает Вашему вниманию коммерческое предложение на следующих условиях:')
    pdf.ln(3)
    total = total_vat = Decimal('0.00')
    with pdf.table(col_widths=(25, 54, 18, 28, 28, 37), text_align=('L', 'L', 'C', 'R', 'R', 'R'),
                   line_height=5, padding=1) as table:
        table.row(('Артикул', 'Наименование', 'Кол-во', 'Цена, руб.', 'Сумма, руб.', 'В т.ч. НДС, руб.'))
        for line in lines:
            unit, subtotal, vat = line.amounts(discount)
            total += subtotal
            total_vat += vat
            tax = f'{vat:.2f}' + ('\nБез НДС (реестр ПО)' if line.registry else '')
            table.row((line.sku, line.name, str(line.quantity), f'{unit:.2f}', f'{subtotal:.2f}', tax))
        table.row(('Итого', '', '', '', f'{total:.2f}', f'{total_vat:.2f}'), style=FontFace(emphasis='B'))

    pdf.ln(4)
    if discount:
        paragraph(f'Скидка {discount.normalize():f}% включена в стоимость.')
    paragraph('Цена указана с учетом всех налогов, сборов, и других обязательных платежей.')
    paragraph(f'Условия оплаты: {payment}.')
    paragraph(f'Срок поставки: {delivery.rstrip(".")}.')
    expires = (today or date.today()) + timedelta(days=14)
    paragraph(f'Срок действия коммерческого предложения - до {expires:%d.%m.%Y}.')
    paragraph('Примечание: Предварительное КП сформировано ИИ-ассистентом для согласования с менеджером ПАО «Софтлайн».')

    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix='.kp_', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        pdf.output(str(temporary))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return total
