"""Fleet administration and device-scoped notice endpoints; no shell in HTTP."""
from fleet_store import FleetRepository
from software_packages import SoftwareError


class FleetHTTP:
    def fleet_get(self):
        from management import Problem
        try:
            self.admin_user(admin_only=True)
            return self.send_json(200, FleetRepository(self.store.data).state())
        except (SoftwareError, Problem) as exc:
            return self.send_json(exc.status, {'error': str(exc)})

    def fleet_post(self, route):
        from management import Problem
        try:
            user = self.admin_user(mutate=True, admin_only=True)
            body = self.read_body(); repo = FleetRepository(self.store.data)
            actor, revision = user['name'], body.get('revision')
            if route == '/api/admin/fleet/enroll':
                result = repo.enroll(body, actor, revision)
            elif route == '/api/admin/fleet/probe':
                result = repo.probe(body.get('id'), actor, revision)
            elif route == '/api/admin/fleet/preflight':
                result = repo.preflight(body, actor, revision)
            elif route == '/api/admin/fleet/execute':
                result = repo.execute(body.get('id'), actor, revision, body.get('confirm'))
            elif route == '/api/admin/fleet/cancel':
                result = repo.cancel_batch(body.get('id'), actor, revision)
            elif route == '/api/admin/fleet/messages/publish':
                result = repo.publish(body, actor, revision)
            elif route == '/api/admin/fleet/messages/withdraw':
                result = repo.withdraw(body.get('id'), revision)
            else:
                raise SoftwareError('批量运维接口不存在', 404)
            with self.store.db() as db:
                self.store.audit(db, actor, '批量运维 '+route.rsplit('/', 1)[-1], result.get('id') or body.get('id') or '')
            return self.send_json(200, result)
        except (SoftwareError, Problem) as exc:
            return self.send_json(exc.status, {'error': str(exc)})

    def fleet_tv_get(self, device):
        return self.send_json(200, FleetRepository(self.store.data).pending_messages(device))

    def fleet_tv_post(self, route, device):
        from management import Problem
        try:
            body = self.read_body(32*1024); repo = FleetRepository(self.store.data)
            result = repo.report(device, body) if route == '/api/v1/managed-status' else repo.receipt(device, body)
            return self.send_json(200, result)
        except (SoftwareError, Problem) as exc:
            return self.send_json(exc.status, {'error': str(exc)})
