"""Payment processor constants."""

CARD_TYPES = {
    'american_express': {
        'display_name': 'American Express',
        'cybersource_code': '003'
    },
    'discover': {
        'display_name': 'Discover',
        'cybersource_code': '004'
    },
    'mastercard': {
        'display_name': 'MasterCard',
        'cybersource_code': '002'
    },
    'visa': {
        'display_name': 'Visa',
        'cybersource_code': '001'
    },
}

CARD_TYPE_CHOICES = ((key, value['display_name']) for key, value in CARD_TYPES.items())
CYBERSOURCE_CARD_TYPE_MAP = {
    value['cybersource_code']: key for key, value in CARD_TYPES.items() if 'cybersource_code' in value
}
