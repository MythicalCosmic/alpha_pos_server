"""Owner-app push hooks. They run after the business transaction commits, so a
push problem can never roll back an expense, a shift or a sync batch."""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from base.models import Shift
from hr.models import Expense, ExpenseTransition


@receiver(post_save, sender=ExpenseTransition, dispatch_uid='owner_push_expense_pending')
def expense_transition_saved(sender, instance, created, **kwargs):
    if created and instance.new_status == Expense.Status.PENDING and not instance.previous_status:
        from admins.services.owner_push import on_expense_pending

        expense_id = instance.expense_id
        transaction.on_commit(lambda: on_expense_pending(expense_id), robust=True)


@receiver(post_save, sender=Shift, dispatch_uid='owner_push_shift_closed')
def shift_saved(sender, instance, **kwargs):
    if instance.status == Shift.Status.ENDED:
        from admins.services.owner_push import on_shift_closed

        shift_id = instance.pk
        transaction.on_commit(lambda: on_shift_closed(shift_id), robust=True)
