"""Customer support — contact channels + a small static FAQ, and ticket threads.

Contacts come from BotConfig (support_phone / support_telegram / support_email);
the FAQ is a static trilingual list. Tickets are owned by a Customer: every
ticket carries a thread of SupportMessages (the first one from the CUSTOMER).
"""
from base.helpers.response import ServiceResponse
from smartfood.models import BotConfig, SupportMessage, SupportTicket

# Static, trilingual FAQ (uz/ru/en per entry) shown alongside the contacts.
_FAQ = [
    {
        'q': {
            'uz': 'Buyurtmani qanday beraman?',
            'ru': 'Как сделать заказ?',
            'en': 'How do I place an order?',
        },
        'a': {
            'uz': 'Menyudan mahsulot tanlang, savatga qoʻshing va buyurtmani tasdiqlang.',
            'ru': 'Выберите блюда из меню, добавьте в корзину и подтвердите заказ.',
            'en': 'Pick items from the menu, add them to the cart and confirm your order.',
        },
    },
    {
        'q': {
            'uz': 'Yetkazib berish qancha vaqt oladi?',
            'ru': 'Сколько времени занимает доставка?',
            'en': 'How long does delivery take?',
        },
        'a': {
            'uz': 'Yetkazib berish odatda manzilingizga qarab 30–60 daqiqa davom etadi.',
            'ru': 'Доставка обычно занимает 30–60 минут в зависимости от адреса.',
            'en': 'Delivery usually takes 30–60 minutes depending on your address.',
        },
    },
    {
        'q': {
            'uz': 'Qanday toʻlov turlari mavjud?',
            'ru': 'Какие способы оплаты доступны?',
            'en': 'Which payment methods are available?',
        },
        'a': {
            'uz': 'Hozircha toʻlov kuryerga naqd pul orqali amalga oshiriladi.',
            'ru': 'Пока оплата принимается наличными курьеру.',
            'en': 'For now, payment is accepted in cash to the courier.',
        },
    },
]


def _message_dict(m):
    return {
        'id': m.id,
        'sender': m.sender,
        'text': m.text,
        'created_at': m.created_at.isoformat() if m.created_at else None,
    }


def _ticket_dict(t):
    return {
        'id': t.id,
        'subject': t.subject,
        'status': t.status,
        'created_at': t.created_at.isoformat() if t.created_at else None,
        'messages': [_message_dict(m) for m in t.messages.all()],
    }


class SupportService:
    @staticmethod
    def contacts():
        cfg = BotConfig.load()
        return ServiceResponse.success(data={
            'contacts': {
                'phone': cfg.support_phone,
                'telegram': cfg.support_telegram,
                'email': cfg.support_email,
            },
            'faq': _FAQ,
        })

    @staticmethod
    def create_ticket(customer, subject, text):
        text = (text or '').strip()
        if not text:
            return ServiceResponse.validation_error({'text': 'required'}, 'A message is required')
        ticket = SupportTicket.objects.create(
            customer=customer, subject=(subject or '')[:160], status=SupportTicket.Status.OPEN)
        SupportMessage.objects.create(
            ticket=ticket, sender=SupportMessage.Sender.CUSTOMER, text=text)
        ticket = SupportTicket.objects.prefetch_related('messages').get(id=ticket.id)
        return ServiceResponse.created(data=_ticket_dict(ticket))

    @staticmethod
    def add_message(customer, ticket_id, text):
        text = (text or '').strip()
        if not text:
            return ServiceResponse.validation_error({'text': 'required'}, 'A message is required')
        ticket = SupportTicket.objects.filter(id=ticket_id, customer=customer).first()
        if not ticket:
            return ServiceResponse.not_found('Ticket not found')
        SupportMessage.objects.create(
            ticket=ticket, sender=SupportMessage.Sender.CUSTOMER, text=text)
        ticket = SupportTicket.objects.prefetch_related('messages').get(id=ticket.id)
        return ServiceResponse.success(data=_ticket_dict(ticket))

    @staticmethod
    def list_tickets(customer):
        tickets = (SupportTicket.objects.filter(customer=customer)
                   .prefetch_related('messages').order_by('-id'))
        return ServiceResponse.success(data={'items': [_ticket_dict(t) for t in tickets]})
