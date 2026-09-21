"""数据迁移对外稳定的异常类型。"""


class IntegrityError(RuntimeError):
    """源数据或迁移结果无法证明完整。"""


class MigrationError(RuntimeError):
    """迁移未完成，原目标数据库仍可继续使用。"""
