import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import JsonObject, Tool, ToolExecutionError


WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


class DateTimeTool(Tool):
    name = "get_current_time"
    description = "获取指定 IANA 时区的当前日期、时间和星期。"
    retryable = True
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA 时区名称，例如 Asia/Shanghai；默认 Asia/Shanghai。",
            }
        },
        "additionalProperties": False,
    }

    def execute(self, arguments: JsonObject) -> str:
        timezone_name = arguments.get("timezone", "Asia/Shanghai")
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            raise ToolExecutionError("timezone 必须是非空字符串。")

        timezone_name = timezone_name.strip()
        try:
            tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            if timezone_name == "Asia/Shanghai":
                tz = timezone(timedelta(hours=8), name="Asia/Shanghai")
            else:
                raise ToolExecutionError(f"未知时区：{timezone_name}") from error

        now = datetime.now(tz)
        return json.dumps(
            {
                "timezone": timezone_name,
                "datetime": now.isoformat(timespec="seconds"),
                "weekday": WEEKDAYS[now.weekday()],
            },
            ensure_ascii=False,
        )
