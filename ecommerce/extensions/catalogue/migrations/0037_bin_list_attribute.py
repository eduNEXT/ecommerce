# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations
from oscar.core.loading import get_model

from ecommerce.core.constants import SEAT_PRODUCT_CLASS_NAME

ProductClass = get_model('catalogue', 'ProductClass')
ProductAttribute = get_model('catalogue', 'ProductAttribute')

def create_bin_list_attribute(apps, schema_editor):

    seat = ProductClass.objects.get(name=SEAT_PRODUCT_CLASS_NAME)

    ProductAttribute.objects.create(
        product_class=seat,
        name='allowed_bin',
        code='allowed_bin',
        type='text',
        required=False,
    )


def delete_bin_list_attribute(apps, schema_editor):
    """For backward compatibility"""
    ProductAttribute.objects.filter(code='allowed_bin').delete()

class Migration(migrations.Migration):

    dependencies = [
        ('catalogue', '0036_coupon_notify_email_attribute')
    ]
    operations = [
        migrations.RunPython(create_bin_list_attribute, delete_bin_list_attribute)
    ]

