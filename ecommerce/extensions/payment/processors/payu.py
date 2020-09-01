# -*- coding: utf-8 -*-
""" PayU payment processing. """
import logging
import json
import requests
from decimal import Decimal
from hashlib import md5
from urlparse import urljoin

from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from oscar.apps.payment.exceptions import GatewayError, TransactionDeclined
from oscar.core.loading import get_model
from requests.exceptions import ConnectionError, Timeout
from slumber.exceptions import SlumberBaseException

from ecommerce.core.models import User
from ecommerce.core.url_utils import get_ecommerce_url, get_lms_url
from ecommerce.extensions.basket.utils import apply_voucher_on_basket_and_check_discount, validate_voucher
from ecommerce.extensions.payment.exceptions import InvalidSignatureError
from ecommerce.extensions.payment.processors import BasePaymentProcessor, HandledProcessorResponse


logger = logging.getLogger(__name__)

PaymentEvent = get_model('order', 'PaymentEvent')
PaymentEventType = get_model('order', 'PaymentEventType')
ProductClass = get_model('catalogue', 'ProductClass')
Source = get_model('payment', 'Source')
SourceType = get_model('payment', 'SourceType')


class Payu(BasePaymentProcessor):
    """
    PayU payment processor (November 2016)

    For reference, see
    http://developers.payulatam.com/en/web_checkout/integration.html
    """

    NAME = 'payu'
    TRANSACTION_ACCEPTED = '4'
    TRANSACTION_DECLINED = '6'
    TRANSACTION_ERROR = '104'
    PAYMENT_FORM_SIGNATURE = 1
    CONFIRMATION_SIGNATURE = 2
    DESCRIPTION_PREFIX = u'Inscripción'
    DESCRIPTION_SEPARATOR = ' | '
    MAX_SPLITS = 1

    def __init__(self, site):
        """
        Constructs a new instance of the PayU processor.

        Raises:
            KeyError: If no settings configured for this payment processor
            AttributeError: If LANGUAGE_CODE setting is not set.
        """
        super(Payu, self).__init__(site)
        configuration = self.configuration
        self.payment_page_url = configuration['payment_page_url']
        self.merchant_id = configuration['merchant_id']
        self.account_id = configuration['account_id']
        self.api_key = configuration['api_key']
        self.tax = configuration['tax']
        self.tax_return_base = configuration['tax_return_base']

        try:
            self.test = configuration['test']
        except KeyError:
            # This is the case for production mode
            self.test = None

        self.response_url = get_ecommerce_url(u'/payment/payu/notify/')
        self.confirmation_url = get_ecommerce_url(u'/payment/payu/notify/')

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        """
        Generate a dictionary of signed parameters PayU requires to complete a transaction.

        Arguments:
            basket (Basket): The basket of products being purchased.

        Keyword Arguments:
            request (Request): A Request object which could be used to construct an absolute URL; not
                used by this method.

        Returns:
            dict: PayU-specific parameters required to complete a transaction, including a signature.
        """

        # Forcing vouchers cleaning.
        # This is to prevent users to submit a basket with not valid vouchers
        self.apply_basket_vouchers_cleaning(basket, request)

        self._verify_student(request.site, request.user)
        parameters = {
            'payment_page_url': self.payment_page_url,
            'merchantId': self.merchant_id,
            'accountId': self.account_id,
            'ApiKey': self.api_key,
            'referenceCode': basket.order_number,
            'tax': self.tax,
            'taxReturnBase': self.tax_return_base,
            'currency': basket.currency,
            'buyerEmail': basket.owner.email,
            'buyerFullName': basket.owner.full_name,
            'amount': str(basket.total_incl_tax),
            'responseUrl': self.response_url,
            'confirmationUrl': self.confirmation_url,
        }

        description = u'{} {}'.format(self.DESCRIPTION_PREFIX, self.get_description(basket))
        if description:
            parameters['description'] = description

        dni, name = self.get_user_details(request, basket)
        if dni:
            parameters['payerDocument'] = dni

        if name:
            # passing the same name for buyer and payer as requested by campus romero
            parameters['payerFullName'] = name
            parameters['buyerFullName'] = name

        if self.test:
            parameters['test'] = self.test

        allowed_bin = basket.get_allowed_bin()
        if allowed_bin:
            parameters['iin'] = allowed_bin

        parameters['signature'] = self._generate_signature(parameters, self.PAYMENT_FORM_SIGNATURE)

        return parameters

    def apply_basket_vouchers_cleaning(self, basket, request):
        """
        Apply voucher basket cleaning to prevent submitting a basket with invalid vouchers
        """
        if basket.num_lines == 1 and basket.lines.first().product.is_enrollment_code_product:
            basket.clear_vouchers()
        elif basket.contains_a_voucher:
            for voucher in basket.vouchers.all():
                basket.vouchers.remove(voucher)
                is_valid, message = validate_voucher(voucher, request.user, basket, request.site)
                if is_valid:
                    apply_voucher_on_basket_and_check_discount(voucher, request, basket)
                else:
                    logger.info(message)

    def get_description(self, basket):
        """
        Returns a unified description for all the products in the basket.
        """
        try:
            seat_class = ProductClass.objects.get(slug='seat')
        except ProductClass.DoesNotExist:
            # this occurs in test configurations where the seat product class is not in use
            return None

        descriptions = []
        for line in basket.lines.all():
            if line.product.get_product_class() == seat_class:
                # Assuming a course locator with the following format: course-v1:CVR+SAC01+2019
                # we are extracting the course_id and run part of it
                # returning SAC01+2019, for the example given above.
                # This must be done for every course in the basket.
                splitted_course_id = line.product.course_id.split('+', self.MAX_SPLITS)
                descriptions.append(splitted_course_id[1].replace('+', '/'))

        return self.DESCRIPTION_SEPARATOR.join(descriptions)

    @staticmethod
    def get_user_details(request, basket):
        """
        Returns the buyer user (edxapp user) details
        Returns None if an exception occurs
        """
        try:
            buyer_user = User.objects.get(email=basket.owner.email)
            response = buyer_user.account_details(request)
            dni = next(field for field in response['extended_profile'] if field["field_name"] == "dni")
            return dni['field_value'], response['name']
        except (ConnectionError, SlumberBaseException, Timeout, StopIteration, KeyError, ObjectDoesNotExist):
            logger.exception(
                'Failed to retrieve user details for [%s]',
                basket.owner.email
            )
            return None, None

    def handle_processor_response(self, response, basket=None):
        """
        Handle a response (i.e., "merchant notification") from PayU.

        This method does the following:
            1. Verify the validity of the response.
            2. Create PaymentEvents and Sources for successful payments.

        Arguments:
            response (dict): Dictionary of parameters received from the payment processor.

        Keyword Arguments:
            basket (Basket): Basket being purchased via the payment processor.

        Raises:
            TransactionDeclined: Indicates the payment was declined by the processor.
            GatewayError: Indicates a general error on the part of the processor.
            InvalidPayUDecision: Indicates an unknown decision value
        """

        # Validate the signature
        if not self.is_signature_valid(response):
            raise InvalidSignatureError

        # Raise an exception for payments that were not accepted. Consuming code should be responsible for handling
        # and logging the exception.
        transaction_state = response['state_pol']
        if transaction_state != self.TRANSACTION_ACCEPTED:
            exception = {
                self.TRANSACTION_DECLINED: TransactionDeclined,
                self.TRANSACTION_ERROR: GatewayError
            }.get(transaction_state, InvalidPayUDecision)

            raise exception

        currency = response.get('currency')
        total = Decimal(response.get('value'))
        transaction_id = response.get('transaction_id')
        card_number = response.get('cc_number', '')
        card_type = response.get('lapPaymentMethod', '')

        return HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=card_number,
            card_type=card_type,
        )

    def _generate_signature(self, parameters, signature_type):
        """
        Sign the contents of the provided transaction parameters dictionary.

        This allows PayU to verify that the transaction parameters have not been tampered with
        during transit.

        We also use this signature to verify that the signature we get back from PayU is valid for
        the parameters that they are giving to us.

        Arguments:
            parameters (dict): A dictionary of transaction parameters.

        Returns:
            unicode: the signature for the given parameters
        """

        # signatures to validate payment form
        if signature_type == self.PAYMENT_FORM_SIGNATURE:
            uncoded = "{api_key}~{merchant_id}~{reference_code}~{amount}~{currency}".format(
                api_key=self.api_key,
                merchant_id=self.merchant_id,
                reference_code=parameters['referenceCode'],
                amount=parameters['amount'],
                currency=parameters['currency'],
            )
            if parameters.get('iin', None):
                uncoded = uncoded + "~{iin}".format(iin=parameters['iin'])

        # PayU applies a logic to validate signatures on confirmation page:
        # If the second decimal of the value parameter is zero, e.g. 150.00
        # the parameter new_value to generate the signature should only have one decimal, as follows: 150.0
        # If the second decimal of the value parameter is different from zero, e.g. 150.26
        # the parameter new_value to generate the signature should have two decimals, as follows: 150.26
        # See http://developers.payulatam.com/en/web_checkout/integration.html
        # signatures to validate confirmation response
        if signature_type == self.CONFIRMATION_SIGNATURE:
            value = parameters['value']
            last_decimal = value[-1]
            if last_decimal == '0':
                new_value = value[:-1]
            else:
                new_value = value

            uncoded = "{api_key}~{merchant_id}~{reference_sale}~{new_value}~{currency}~{state_pol}".format(
                api_key=self.api_key,
                merchant_id=self.merchant_id,
                reference_sale=parameters['reference_sale'],
                new_value=new_value,
                currency=parameters['currency'],
                state_pol=parameters['state_pol'],
            )

        return md5(uncoded).hexdigest()

    def is_signature_valid(self, response):
        """Returns a boolean indicating if the response's signature (indicating potential tampering) is valid."""
        return response and (self._generate_signature(response, self.CONFIRMATION_SIGNATURE) == response.get('sign'))

    def issue_credit(self, order, reference_number, amount, currency):  # pylint: disable=unused-argument
        """
        This method should be implemented in the future in order
        to accept payment refunds
        see http://developers.payulatam.com/en/api/refunds.html
        """

        logger.exception(
            'PayU processor can not issue credits or refunds',
        )

        raise NotImplementedError

    def _verify_student(self, site, username):
        """
        Verify the student identity.

        This method is traversal to the platform, it updates the user verification data
        and it's not dependant of the course that is being purchased.

        This is a bad practice, since it is forcefully verifying the user and it does so
        in a unrelated point of the code.

        """
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


class InvalidPayUDecision(GatewayError):
    """The decision returned by PayU was not recognized."""
    pass
