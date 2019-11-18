# -*- coding: utf-8 -*-
""" PayU payment processing.. """
from decimal import Decimal
from datetime import datetime
import pytz
import uuid
import logging
from urlparse import urljoin
from django.conf import settings
from django.core.urlresolvers import reverse
from django.utils.functional import cached_property
from oscar.apps.payment.exceptions import UserCancelled, GatewayError, TransactionDeclined
from ecommerce.extensions.payment.exceptions import (InvalidSignatureError, InvalidPayUStatus, PartialAuthorizationError)
from ecommerce.core.url_utils import get_ecommerce_url, get_lms_url
from ecommerce.extensions.payment.processors import BasePaymentProcessor, HandledProcessorResponse
from ecommerce.extensions.payment.utils import middle_truncate
import hashlib
import time
import requests
import json

logger = logging.getLogger(__name__)
'''
PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
ProductClass = get_model('catalogue', 'ProductClass')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')
'''

class PayU(BasePaymentProcessor):
    """
    PayU REST API (May 2017)
    """

    NAME = u'payu'
    DEFAULT_PROFILE_NAME = 'default'

    def __init__(self, site):
        """
        Constructs a new instance of the PayU processor.

        Raises:
            KeyError: If a required setting is not configured for this payment processor
        """
        super(PayU, self).__init__(site)
        configuration = self.configuration
        self.retry_attempts = configuration.get('retry_attempts', 1)
        self.merchant_id = unicode(configuration['merchant_id'])
        self.account_id = unicode(configuration['account_id'])
        self.api_key = configuration['api_key']
        self.payment_page_url = configuration['payment_page_url']
        self.language_code = settings.LANGUAGE_CODE

        try:
            self.test = configuration['test']
        except KeyError:
            # This is the case for production mode
            self.test = None

    @property
    def dashboard_url(self):
        return get_lms_url(u'/dashboard')

    @property
    def confirmation_url(self):
        return get_ecommerce_url(u'/payment/payu/confirmation/')

    @property
    def cancel_url(self):
        return get_ecommerce_url(self.configuration['cancel_checkout_path'])

    @property
    def error_url(self):
        return get_ecommerce_url(self.configuration['error_path'])

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        """
        Create a new PayU payment.

        Arguments:
            basket (Basket): The basket of products being purchased.

        Keyword Arguments:
            request (Request): A Request object which is used to construct PayU's `return_url`.

        Returns:
            dict: PayU-specific parameters required to complete a transaction. Must contain a URL
                to which users can be directed in order to approve a newly created payment.

        Raises:
            GatewayError: Indicates a general error or unexpected behavior on the part of PayU which prevented
                a payment from being created.
        """
        user = request.user
        return_url = urljoin(get_ecommerce_url(), reverse('payu:payu_execute'))


        _line = basket.all_lines()[0]
        logging.info(basket.all_lines().__dict__)
        _split_course = _line.product.course_id.split('+')
        course_id = _split_course[1]+'/'+_split_course[2]

        data = {
            'intent': 'sale',
            'redirect_urls': {
                'course_id': course_id,
                'return_url': return_url,
                'cancel_url': self.cancel_url,
                'dashboard_url': self.dashboard_url,
            },
            'payer': {
                'payment_method': 'payu',
            },
            'transactions': [{
                'amount': {
                    'total': unicode(basket.total_incl_tax),
                    'currency': basket.currency,
                },
                'item_list': {
                    'items': [
                        {
                            'quantity': line.quantity,
                            'name': middle_truncate(line.product.title, 127),
                            'price': unicode(line.line_price_incl_tax_incl_discounts / line.quantity),
                            'currency': line.stockrecord.price_currency,
                        }
                        for line in basket.all_lines()
                    ],
                },
                'invoice_number': basket.order_number,
            }],
        }
        entry = self.record_processor_response(data, transaction_id=basket.order_number, basket=basket)
        logger.info(u"Successfully created PayU for basket [%d], user [%s].", basket.id, user)

        self._verify_student(request.site,user)

        # STATUS: REJECTED/PENDING/APPROVED DECLINED, ERROR, EXPIRED
        responseUrl='{}?orderNum={}'.format(return_url, basket.order_number)
        confirmationUrl=self.confirmation_url
        # VD
        fechaActual = time.strftime("%H%M%S")
        codigoReferencia = str(basket.order_number) + str(fechaActual)
        # --
        parameters = {
            'merchantId': self.merchant_id,
            'accountId': self.account_id,
            'description': u'Inscripción {}'.format(course_id),
            'referenceCode': codigoReferencia,
            'amount': str(basket.total_incl_tax),
            'tax': '0',
            'taxReturnBase': '0',
            'currency': basket.currency,
            'signature': self._generate_signature(self.api_key, self.merchant_id, codigoReferencia, str(basket.total_incl_tax), str(basket.currency)),
            'buyerEmail': user.email,
            'responseUrl': responseUrl,
            'confirmationUrl': confirmationUrl
        }
        parameters['payment_page_url'] = self.payment_page_url

        if self.test:
            parameters['test'] = self.test

        return parameters

    def handle_processor_response(self, response, basket=None):
        available_attempts = 1

        transaction_id = response['transactionId']
        transactionState = response['transactionState']
        status = response['lapTransactionState'] #REJECTED/PENDING/APPROVED DECLINED, ERROR, EXPIRED

        self.record_processor_response(response, transaction_id=transaction_id, basket=basket)
        logger.info(u"Successfully executed PayU payment [%s] for basket [%d].", transaction_id, basket.id)

        if transactionState != '4':
            exception = {
                'cancel': UserCancelled,
                'decline': TransactionDeclined,
                'error': GatewayError
            }.get(status, InvalidPayUStatus)

            raise exception

        currency = response['currency']
        total = Decimal(response['TX_VALUE'])
        email = basket.owner.email
        label = 'PayU ({})'.format(email) if email else 'PayU Account'

        return HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=label,
            card_type=None
        )

    def issue_credit(self, source, amount, currency):
        """
        This method should be implemented in the future in order
        to accept payment refunds
        see http://developers.payulatam.com/en/api/refunds.html
        """

        logger.exception(
            'PayU processor can not issue credits or refunds',
        )

        raise NotImplementedError

    def _generate_signature(self, ApiKey, merchantId, referenceCode, amount, currency):
        firma = hashlib.md5()
        firma.update(ApiKey + "~" + merchantId + "~" + referenceCode + "~" + amount + "~" + currency)
        wfirma = firma.hexdigest()

        return wfirma

    def _verify_student(self, site, username):
        path_api = settings.OPENEDX_EXTENSIONS_API_URL
        url = urljoin(get_lms_url(path_api), "change_to_verified_mode/")
        access_token = site.siteconfiguration.access_token

        headers = {
            "authorization": "JWT {}".format(access_token),
            "Content-Type": "application/json"
        }

        data = json.dumps({
            "username": username.username
        })

        try:
            response = requests.request("POST", url, data=data, headers=headers)
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            raise err
