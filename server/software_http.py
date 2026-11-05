"""Admin-only software endpoints. Installation is exclusively an external worker task."""
import os
from pathlib import Path
import secrets
from urllib.parse import unquote
from software_packages import MAX_UPLOAD, SoftwareError
from software_store import Repository


class SoftwareHTTP:
    def software_repository(self):
        return Repository(self.store.data)

    def software_post(self, route):
        from management import Problem
        try:
            user = self.admin_user(mutate=True, admin_only=True)
            repo = self.software_repository()
            if route == '/api/admin/software/upload':
                if self.headers.get('Transfer-Encoding'):
                    raise SoftwareError('上传需要明确 Content-Length', 411)
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise SoftwareError('上传大小无效')
                if not 0 < size <= MAX_UPLOAD:
                    raise SoftwareError('安装包为空或超过 2 GiB', 413)
                name = unquote(self.headers.get('X-Filename', ''))
                if not name or len(name) > 200 or Path(name).name != name or '\\' in name or any(ord(c) < 32 for c in name):
                    raise SoftwareError('安装包文件名无效')
                incoming = self.store.data / 'software' / 'incoming'
                incoming.mkdir(parents=True, exist_ok=True)
                import shutil
                if shutil.disk_usage(incoming).free < size + 256 * 1024 ** 2:
                    raise SoftwareError('服务器剩余空间不足，请先释放安装包存储空间', 409)
                path = incoming / (secrets.token_hex(16) + '.part')
                try:
                    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                    with os.fdopen(fd, 'wb') as output:
                        remaining = size
                        while remaining:
                            block = self.rfile.read(min(1024 * 1024, remaining))
                            if not block:
                                raise SoftwareError('上传中断，未创建软件版本')
                            output.write(block); remaining -= len(block)
                    item = repo.register(path, name, user['name'])
                finally:
                    path.unlink(missing_ok=True)
                self.software_audit(user, '上传软件安装包', item['software_id'] + ' ' + item['version'])
                return self.send_json(201, {'package': item})
            body = self.read_body()
            if route == '/api/admin/software/install':
                if body.get('confirm') is not True:
                    raise SoftwareError('请先核对软件版本与安装目标并确认')
                result = repo.queue(body.get('package_id'), body.get('targets'), user['name'], body.get('revision'), body.get('request_id'))
                repo.notify(result['jobs'])
                self.software_audit(user, '手动安装软件', ','.join(j['id'] for j in result['jobs']))
            elif route == '/api/admin/software/target':
                result = repo.save_target(body, user['name'], body.get('revision'))
                self.software_audit(user, '登记电视维护连接', result['id'])
            elif route == '/api/admin/software/job':
                result = repo.control(body.get('id'), body.get('action'), user['name'], body.get('revision'), body.get('request_id', ''))
                repo.notify(result.get('jobs', []))
                self.software_audit(user, '软件任务 ' + str(body.get('action')), str(body.get('id')))
            else:
                raise SoftwareError('软件管理接口不存在', 404)
            return self.send_json(200, result)
        except (SoftwareError, Problem) as exc:
            return self.send_json(exc.status, {'error': str(exc)})
        except (ValueError, TypeError, OSError):
            return self.send_json(400, {'error': '软件操作失败，请检查安装包、输入及可用空间'})

    def software_audit(self, user, action, detail):
        with self.store.db() as db:
            self.store.audit(db, user['name'], action, detail)
