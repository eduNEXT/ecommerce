
from django.conf.urls import include, url

from ecommerce.extensions.payment.views import PaymentFailedView, SDNFailure, cybersource, paypal
from ecommerce.extensions.payment.views.payu import PayuPaymentExecutionView, PayuConfirmationExecutionView #, PayuProfileAdminView


CYBERSOURCE_URLS = [
    url(r'^redirect/$', cybersource.CybersourceInterstitialView.as_view(), name='redirect'),
    url(r'^submit/$', cybersource.CybersourceSubmitView.as_view(), name='submit'),
]

PAYPAL_URLS = [
    url(r'^execute/$', paypal.PaypalPaymentExecutionView.as_view(), name='execute'),
    url(r'^profiles/$', paypal.PaypalProfileAdminView.as_view(), name='profiles'),
]

SDN_URLS = [
    url(r'^failure/$', SDNFailure.as_view(), name='failure'),
]

urlpatterns = [
    url(r'^cybersource/', include(CYBERSOURCE_URLS, namespace='cybersource')),
    url(r'^error/$', PaymentFailedView.as_view(), name='payment_error'),
    url(r'^paypal/', include(PAYPAL_URLS, namespace='paypal')),
    url(r'^sdn/', include(SDN_URLS, namespace='sdn')),
    url(r'^paypal/', include(PAYPAL_URLS, namespace='paypal')),
    url(r'^paypal/', include(PAYPAL_URLS, namespace='paypal')),  
    url(r'^payu/execute/$', PayuPaymentExecutionView.as_view(), name='payu_execute'), #MH
    url(r'^payu/confirmation/$', PayuConfirmationExecutionView.as_view(), name='payu_confirmation'), #MH

]

""" Payment-related URLs """
'''
from django.conf.urls import url

from ecommerce.extensions.payment.views import cybersource, PaymentFailedView
from ecommerce.extensions.payment.views.paypal import PaypalPaymentExecutionView, PaypalProfileAdminView
from ecommerce.extensions.payment.views.payu import PayuPaymentExecutionView, PayuConfirmationExecutionView #, PayuProfileAdminView

urlpatterns = [
    url(r'^cybersource/notify/$', cybersource.CybersourceNotifyView.as_view(), name='cybersource_notify'),
    url(r'^cybersource/redirect/$', cybersource.CybersourceInterstitialView.as_view(), name='cybersource_redirect'),
    url(r'^cybersource/submit/$', cybersource.CybersourceSubmitView.as_view(), name='cybersource_submit'),
    url(r'^error/$', PaymentFailedView.as_view(), name='payment_error'),
    url(r'^paypal/execute/$', PaypalPaymentExecutionView.as_view(), name='paypal_execute'),
    url(r'^paypal/profiles/$', PaypalProfileAdminView.as_view(), name='paypal_profiles'),
    url(r'^payu/execute/$', PayuPaymentExecutionView.as_view(), name='payu_execute'), #MH
    #url(r'^payu/profiles/$', PayuProfileAdminView.as_view(), name='payu_profiles'), #MH
    url(r'^payu/confirmation/$', PayuConfirmationExecutionView.as_view(), name='payu_confirmation'), #MH
]
'''