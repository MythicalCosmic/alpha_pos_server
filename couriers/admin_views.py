"""Back-office endpoints the desktop POS calls to dispatch a delivery to a
courier. Session-auth'd as staff (ADMIN/MANAGER), mounted under
/api/admins/couriers/."""
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from base.helpers.request import parse_json_body
from base.security.permissions import manager_required
from base.models import Order

from couriers.models import Courier
from couriers import services


@require_GET
@manager_required
def couriers_list(request):
    """Couriers available for assignment (the desktop's picker)."""
    rows = []
    for c in Courier.objects.select_related('user').all():
        rows.append({
            'id': c.code, 'pk': c.id, 'name': c.full_name, 'phone': c.phone,
            'vehicle': c.vehicle, 'plate': c.plate, 'online': c.online,
            'rating': float(c.rating), 'branch': c.branch_name or c.branch_id,
        })
    return JsonResponse({'success': True, 'data': rows})


@csrf_exempt
@require_POST
@manager_required
def assign_order(request):
    """POST /api/admins/couriers/assign
    { order_id, courier (code) | courier_id (pk), fee, addr_text, addr_landmark,
      addr_lat, addr_lng, distance_km } -> emits order.assigned to the courier."""
    data, error = parse_json_body(request)
    if error:
        return JsonResponse(error[0], status=error[1])

    order_id = data.get('order_id')
    if not order_id:
        return JsonResponse({'success': False, 'message': 'order_id required'}, status=400)
    order = get_object_or_404(Order, pk=order_id)

    courier = None
    if data.get('courier_id'):
        courier = Courier.objects.filter(pk=data['courier_id']).first()
    elif data.get('courier'):
        courier = Courier.objects.filter(code=data['courier']).first()
    if not courier:
        return JsonResponse({'success': False, 'message': 'courier not found'}, status=404)

    assignment = services.assign(
        order, courier,
        fee=data.get('fee', 0),
        addr_text=data.get('addr_text', ''),
        addr_landmark=data.get('addr_landmark', ''),
        addr_lat=data.get('addr_lat'),
        addr_lng=data.get('addr_lng'),
        distance_km=data.get('distance_km'),
    )
    return JsonResponse({'success': True, 'message': 'assigned',
                         'data': {'order_id': order.id, 'courier': courier.code,
                                  'step': assignment.step}})
