"""MySQL 访问的共用基础设施。

实现见 :mod:`app.infrastructure.mysql.db`：引擎按 DSN 进程内复用、时间戳的
进出转换、以及 SQLAlchemy/DBAPI 异常到领域错误的分类。
"""
