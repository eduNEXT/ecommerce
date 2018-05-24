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
#from oscar.core.loading import get_model
#import paypalrestsdk
import waffle

from ecommerce.core.url_utils import get_ecommerce_url, get_lms_url
#from ecommerce.extensions.order.constants import PaymentEventTypeName
from ecommerce.extensions.payment.processors import BasePaymentProcessor, HandledProcessorResponse
#from ecommerce.extensions.payment.models import PaypalWebProfile, PaypalProcessorConfiguration ##
#from ecommerce.extensions.payment.models import PayuWebProfile
from ecommerce.extensions.payment.utils import middle_truncate
import hashlib
import time
import json
from django.shortcuts import redirect
#from lms.djangoapps.verify_student.models import SoftwareSecurePhotoVerification

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

    For reference, see https://developer.paypal.com/docs/api/.
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
        return_url = urljoin(get_ecommerce_url(), reverse('payu_execute'))

        
        _line = basket.all_lines()[0]
        logging.info(' YASYYAYAYAYSDYAYSDYASDYAYSDYAS ---------------------------------------------------------------------------------------')
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
        
        self._verify_student(user)
        
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
            #'transaction_uuid': uuid.uuid4().hex,
            'description': u'Inscripción {}'.format(course_id),
            'referenceCode': codigoReferencia,
            'amount': str(basket.total_incl_tax),
            #'locale': self.language_code,
            'tax': '0',
            'taxReturnBase': '0',
            #'reference_number': basket.order_number,
            'currency': basket.currency,
            'signature': self._generate_signature(self.api_key, self.merchant_id, codigoReferencia, str(basket.total_incl_tax), str(basket.currency)),
            'buyerEmail': user.email,
            'responseUrl': responseUrl,
            'confirmationUrl': confirmationUrl
            #'consumer_id': basket.owner.username,
            #'override_custom_receipt_page': '{}?orderNum={}'.format(self.receipt_page_url, basket.order_number),
            #'override_custom_cancel_page': self.cancel_page_url,
        }        
        parameters['payment_page_url'] = self.payment_page_url

        if self.test:
            parameters['test'] = self.test

        return parameters

    def handle_processor_response(self, response, basket=None):
        """
        Execute an approved PayPal payment.

        This method creates PaymentEvents and Sources for approved payments.

        Arguments:
            response (dict): Dictionary of parameters returned by PayPal in the `return_url` query string.

        Keyword Arguments:
            basket (Basket): Basket being purchased via the payment processor.

        Raises:
            GatewayError: Indicates a general error or unexpected behavior on the part of PayPal which prevented
                an approved payment from being executed.
        """
        # By default PayPal payment will be executed only once.
        available_attempts = 1

        transaction_id = response['transactionId']
        #card_number = response['req_card_number']
        transactionState = response['transactionState']
        status = response['lapTransactionState'] #REJECTED/PENDING/APPROVED DECLINED, ERROR, EXPIRED
        
        self.record_processor_response(response, transaction_id=transaction_id, basket=basket)
        logger.info(u"Successfully executed PayU payment [%s] for basket [%d].", transaction_id, basket.id)
        '''
        estadoTx = ''
        if transactionState == '4' :
            estadoTx = 'Transacción aprobada'
        elif transactionState == '6' :
            estadoTx = 'Transacción rechazada'
        elif transactionState == '104' :
            estadoTx = 'Error'
        elif transactionState == '7' :
            estadoTx = 'Transacción pendiente'
        '''
        if transactionState != '4':
            exception = {
                'cancel': UserCancelled,
                'decline': TransactionDeclined,
                'error': GatewayError
            }.get(status, InvalidPayUStatus)

            raise exception

        currency = response['currency']
        total = Decimal(response['TX_VALUE'])
        #tax = Decimal(response['TX_TAX'])
        email = basket.owner.email #request.user.email
        label = 'PayU ({})'.format(email) if email else 'PayU Account'

        #return source, event
        return HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=label,
            card_type=None
        )        

    def _get_error(self, payment):
        """
        Shameful workaround for mocking the `error` attribute on instances of
        `paypalrestsdk.Payment`. The `error` attribute is created at runtime,
        but passing `create=True` to `patch()` isn't enough to mock the
        attribute in this module.
        """
        return payment.error  # pragma: no cover

    def _get_payment_sale(self, payment):
        print '--------_get_payment_sale--------'
        pass

    def issue_credit(self, source, amount, currency):
        print '--------issue_credit--------'
        pass

    def _generate_signature(self, ApiKey, merchantId, referenceCode, amount, currency):
        firma = hashlib.md5()
        # FORMA DEL API KEY DE PAYU: ApiKey~merchantId~referenceCode~amount~currency
        firma.update(ApiKey + "~" + merchantId + "~" + referenceCode + "~" + amount + "~" + currency)
        wfirma = firma.hexdigest()

        return wfirma

    def _verify_student(self, username):
        try:
            fecha = datetime.now(pytz.UTC).isoformat()
            # logger.info(u"verify_student------------>. [%d], [%s].", username,fecha)
            import MySQLdb as mdb
            con = mdb.connect('localhost', 'edxapp001', 'H9UQbugicFARzDlEbtLpNdnjpzpfB7CnuXk', 'edxapp')
            cur = con.cursor()
            cur.execute("SELECT id, first_name, last_name FROM edxapp.auth_user where username=%s", (username,))
            row = cur.fetchone()        
            user_id = row[0]
            user_fullname = row[1] + ' ' + row[2]
            logger.info(u"verify_student--> user_id [%s], user_fullname [%s].", user_id, user_fullname)
            cur.execute("SELECT count(0) from verify_student_softwaresecurephotoverification where status='approved' and user_id=%s", (user_id,))
            row = cur.fetchone()        
            is_approved = row[0]
            logger.info(u"verify_student--> user_id [%s], user_fullname [%s], is_approved [%s].", user_id, user_fullname, is_approved)
            if is_approved == 0:
                receipt_id = str(uuid.uuid4())   
                if not user_fullname:
                    user_fullname = username
                result = cur.execute("INSERT verify_student_softwaresecurephotoverification (status,status_changed,name,face_image_url,photo_id_image_url,receipt_id,created_at,updated_at,display,submitted_at,reviewing_service,error_msg,error_code,photo_id_key,reviewing_user_id,user_id) VALUES ('approved',%s,%s,'','',%s,%s,%s,'1',%s,'','','','fake-photo-id-key',%s,%s)", (fecha,user_fullname,receipt_id,fecha,fecha,fecha,user_id,user_id))
                con.commit()
            cur.close()
            con.close()
        except:
            pass
