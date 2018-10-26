# -*- coding: utf-8 -*-
""" Views for interacting with the payment processor. """
import logging
import os
from cStringIO import StringIO
from decimal import Decimal
from django.views.decorators.csrf import csrf_exempt
from ecommerce.extensions.order.constants import PaymentEventTypeName

from django.core.exceptions import MultipleObjectsReturned
from django.core.management import call_command
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.generic import View
from oscar.apps.partner import strategy
from oscar.apps.payment.exceptions import PaymentError
from oscar.core.loading import get_class, get_model

from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.payment.processors.payu import PayU

logger = logging.getLogger(__name__)

PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
OrderNote = get_model('order', 'OrderNote')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')

Order = get_model('order', 'Order')

Applicator = get_class('offer.utils', 'Applicator')
Basket = get_model('basket', 'Basket')
BillingAddress = get_model('order', 'BillingAddress')
Country = get_model('address', 'Country')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')


class PayuPaymentExecutionView(EdxOrderPlacementMixin, View):
    """Execute an approved PayU payment and place an order for paid products as appropriate."""

    PAYMENT_PENDING = '7'

    @property
    def payment_processor(self):
        return PayU(self.request.site)

    @method_decorator(transaction.non_atomic_requests)
    def dispatch(self, request, *args, **kwargs):
        return super(PayuPaymentExecutionView, self).dispatch(request, *args, **kwargs)

    def _get_basket(self, payment_id):
        """
        Retrieve a basket using a payment ID.

        Arguments:
            payment_id: payment_id received from PayPal.

        Returns:
            It will return related basket or log exception and return None if
            duplicate payment_id received or any other exception occurred.

        """
        try:
            basket = PaymentProcessorResponse.objects.get(
                processor_name=self.payment_processor.NAME,
                transaction_id=payment_id
            ).basket
            basket.strategy = strategy.Default()
            Applicator().apply(basket, basket.owner, self.request)
            return basket
        except MultipleObjectsReturned:
            logger.exception(u"Duplicate payment ID [%s] received from PayPal.", payment_id)
            return None
        except Exception:  # pylint: disable=broad-except
            logger.exception(u"Unexpected error during basket retrieval while executing PayPal payment.")
            return None

    def get(self, request):
        """Handle an incoming user returned to us by PayPal after approving payment."""
        payment_id = request.GET.get('orderNum')
        transactionState = request.GET.get('transactionState')
        logger.info(u"PayU payment [%s] transactionState [%s]", payment_id, transactionState)

        payu_response = request.GET.dict()
        basket = self._get_basket(payment_id)

        if not basket:
            return redirect(self.payment_processor.error_url)

        receipt_url = get_receipt_page_url(
            order_number=basket.order_number,
            site_configuration=basket.site.siteconfiguration
        )

        try:
            with transaction.atomic():
                try:
                    self.handle_payment(payu_response, basket)
                except PaymentError:
                    logger.exception('PaymentError for basket [%d] failed.', basket.id)
                    if transactionState == self.PAYMENT_PENDING:
                        return redirect(self.payment_processor.dashboard_url)
                    else:
                        return redirect(self.payment_processor.error_url)
        except:  # pylint: disable=bare-except
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            return redirect(receipt_url)

        try:
            shipping_method = NoShippingRequired()
            shipping_charge = shipping_method.calculate(basket)
            order_total = OrderTotalCalculator().calculate(basket, shipping_charge)

            user = basket.owner
            """
            Given a basket, order number generation is idempotent. Although we've already
            generated this order number once before, it's faster to generate it again
            than to retrieve an invoice number from PayU.
            """
            order_number = basket.order_number

            self.handle_order_placement(
                order_number=order_number,
                user=user,
                basket=basket,
                shipping_address=None,
                shipping_method=shipping_method,
                shipping_charge=shipping_charge,
                billing_address=None,
                order_total=order_total,
                request=request
            )

            return redirect(receipt_url)
        except:  # pylint: disable=bare-except
            logger.exception(self.order_placement_failure_msg, basket.id)
            return redirect(receipt_url)


class PayuConfirmationExecutionView(EdxOrderPlacementMixin, View):
    @property
    def payment_processor(self):
        return PayU(self.request.site)

    @method_decorator(transaction.non_atomic_requests)
    @method_decorator(csrf_exempt)
    def dispatch(self, request, *args, **kwargs):
        return super(PayuConfirmationExecutionView, self).dispatch(request, *args, **kwargs)

    def post(self, request):
        data = request.POST.dict()
        transaction_id = data.get("transaction_id")
        reference_sale = data.get("reference_sale")
        logger.info(u"PayU transaction_id [%s], reference_sale [%s]", transaction_id, reference_sale)
        statePayU = data.get("response_code_pol")
        logger.info(u"PayU transaction_id [%s], statePayU [%s]", transaction_id, statePayU)

        payment_id = reference_sale[:-6]
        processor_name = self.payment_processor.NAME

        basket = PaymentProcessorResponse.objects.filter(
            processor_name=processor_name,
            transaction_id=payment_id
        )[0].basket
        try:
            if not basket:
                logger.error('Received payment for non-existent basket [%s].', basket_id)
                return HttpResponse(status=400)
        finally:
            ppr = self.payment_processor.record_processor_response(data, transaction_id=transaction_id,basket=basket)

        user = basket.owner
        logger.info(u"PayU payment [%s] approved by payer [%s]", payment_id, user.username)

        if statePayU != "1":
            logger.error('Error de transactionState [%s] en pedido [%s] por el usuario [%s].', statePayU, payment_id, user.username)
            return HttpResponse(status=200)

        ORDER_NUMBER = basket.order_number
        try:
            order = Order.objects.get(number=ORDER_NUMBER)
        except Order.DoesNotExist:
            basket.strategy = strategy.Default()
            Applicator().apply(basket, user, self.request)
            try:
                shipping_method = NoShippingRequired()
                shipping_charge = shipping_method.calculate(basket)
                order_total = OrderTotalCalculator().calculate(basket, shipping_charge)
                user = basket.owner
                order_number = basket.order_number

                self.handle_order_placement(
                    order_number=order_number,
                    user=user,
                    basket=basket,
                    shipping_address=None,
                    shipping_method=shipping_method,
                    shipping_charge=shipping_charge,
                    billing_address=None,
                    order_total=order_total
                )
                order = Order.objects.get(number=ORDER_NUMBER)
            except:
                logger.exception(self.order_placement_failure_msg, basket.id)
                return HttpResponse(status=400)

        with transaction.atomic():
            OrderNote.objects.create(
                order=order, message="Confirmación de PayU de la orden {}".format(ORDER_NUMBER)
            )

        # Get or create Source used to track transactions related to PayU
        source_type, __ = SourceType.objects.get_or_create(name=processor_name)
        currency = data.get("currency")
        total = Decimal(data.get("value"))
        email = user.email
        label = 'PayU ({})'.format(email) if email else 'PayU Account'
        Source.objects.create(
            order=order,
            source_type=source_type,
            currency=currency,
            amount_allocated=total,
            amount_debited=total,
            reference=transaction_id,
            label=label,
            card_type=None
        )
        # Create PaymentEvent to track payment
        event_type, __ = PaymentEventType.objects.get_or_create(name=PaymentEventTypeName.PAID)
        PaymentEvent.objects.create(
            order=order,
            event_type=event_type,
            amount=total,
            reference=transaction_id,
            processor_name=processor_name
        )
        return HttpResponse(status=200)
