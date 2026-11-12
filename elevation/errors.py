"""服务层错误类型，HTTP 层据此映射状态码。"""


class ServiceError(Exception):
    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status


class ValidationError(ServiceError):
    status = 400


class ConflictError(ServiceError):
    status = 409


class OutOfBoundsError(ServiceError):
    status = 422


class PayloadTooLarge(ServiceError):
    status = 413


class StorageError(ServiceError):
    status = 500
