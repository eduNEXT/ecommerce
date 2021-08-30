"""
In this module are defined custom template tags and filters that are meant to be used by the basket/cart page
"""
from decimal import Decimal
from django import template
from django.conf import settings

register = template.Library()


@register.filter(name='calculate_tax')
def calculate_tax(price_excl_tax):
    """
    Calculate the tax that is going to be applied to a product price, rounded up to 2 decimal digits

    Example:
        if settings.TAX_RATE is 0.5 and price_excl_tax is 100, this will return 50 (0.5 * 100)

    Arguments:
        price_excl_tax: Price excluding tax
    """
    return round(Decimal(price_excl_tax) * Decimal(settings.TAX_RATE), 2)


@register.simple_tag
def get_tax_rate():
    """
    Returns the TAX_RATE defined in settings as a percentage(multiplies it by 100.0), rounded up to 2 decimal digits
    """
    tax_rate_as_percentage = Decimal(settings.TAX_RATE) * Decimal(100.0)
    return round(tax_rate_as_percentage, 2)


@register.simple_tag
def calculate_tax_of_order_line(price_excl_tax, price_incl_tax):
    """
    Calculates the order line taxes by taking the price including and excluding tax
    """
    return price_incl_tax - price_excl_tax


@register.simple_tag
def calculate_vat_from_unit_price(unit_price_excl_tax, unit_price_incl_tax):
    """
    Calculates the VAT(Tax Rate) as a percentage(multiplied by 100.0) from order line unit price including tax and
    excluding tax, rounded up to 2 decimal digits
    """
    calculated_tax = Decimal(unit_price_incl_tax) - Decimal(unit_price_excl_tax)
    calculated_tax_as_percentage = (calculated_tax / Decimal(unit_price_excl_tax)) * Decimal(100.0)
    return round(calculated_tax_as_percentage, 2)
