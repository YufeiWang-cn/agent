"""验证 DeepSeek 流式响应到内部事件协议的转换。"""

import unittest
from types import SimpleNamespace

from _path_setup import add_src_to_path

add_src_to_path()

from deepseek_agent.models import (
    DeepSeekModel,
    ModelProtocolError,
    TextDelta,
    ToolCallRequest,
    UsageUpdate,
)


class FakeCompletions:
    """记录请求，并按测试预设返回流式分片。"""

    def __init__(self, chunks) -> None:
        self.chunks = chunks
        self.requests: list[dict[str, object]] = []

    def create(self, **request):
        self.requests.append(request)
        return iter(self.chunks)


def _tool_delta(
    index: int | None,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


def _chunk(
    *,
    content: str | None = None,
    tool_calls=(),
    finish_reason: str | None = None,
    include_choice: bool = True,
    usage=None,
):
    choices = []
    if include_choice:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                ),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


class DeepSeekModelTests(unittest.TestCase):
    def build_model(self, chunks) -> tuple[DeepSeekModel, FakeCompletions]:
        completions = FakeCompletions(chunks)
        model = object.__new__(DeepSeekModel)
        model._model_name = "test-model"
        model._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        return model, completions

    def test_stream_forwards_text_and_request_shape(self) -> None:
        model, completions = self.build_model(
            [
                _chunk(content="你"),
                _chunk(content="好", finish_reason="stop"),
            ]
        )
        messages = [{"role": "user", "content": "测试"}]

        events = list(model.stream(messages, []))

        self.assertEqual(events, [TextDelta("你"), TextDelta("好")])
        self.assertEqual(
            completions.requests,
            [{"model": "test-model", "messages": messages, "stream": True}],
        )

    def test_tool_call_fragments_are_assembled_by_index(self) -> None:
        model, _completions = self.build_model(
            [
                _chunk(
                    tool_calls=[
                        _tool_delta(
                            0,
                            call_id="call_1",
                            name="read_",
                            arguments='{"path":',
                        ),
                        _tool_delta(1, call_id="call_2", name="calculator"),
                    ]
                ),
                _chunk(
                    tool_calls=[
                        _tool_delta(0, name="text_file", arguments='"README.md"}'),
                        _tool_delta(1, arguments='{"expression":"1+1"}'),
                    ],
                    finish_reason="tool_calls",
                ),
            ]
        )

        events = list(model.stream([], [{"type": "function"}]))

        self.assertEqual(
            events,
            [
                ToolCallRequest(
                    id="call_1",
                    name="read_text_file",
                    arguments='{"path":"README.md"}',
                ),
                ToolCallRequest(
                    id="call_2",
                    name="calculator",
                    arguments='{"expression":"1+1"}',
                ),
            ],
        )

    def test_empty_chunks_produce_an_empty_event_stream(self) -> None:
        model, _completions = self.build_model(
            [_chunk(include_choice=False), _chunk(finish_reason="stop")]
        )

        self.assertEqual(list(model.stream([], [])), [])

    def test_usage_chunk_is_forwarded_even_without_choices(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=120,
            completion_tokens=30,
            total_tokens=150,
        )
        model, _completions = self.build_model(
            [_chunk(include_choice=False, usage=usage)]
        )

        self.assertEqual(
            list(model.stream([], [])),
            [UsageUpdate(120, 30, 150)],
        )

    def test_missing_tool_name_is_reported_as_protocol_error(self) -> None:
        model, _completions = self.build_model(
            [
                _chunk(
                    tool_calls=[_tool_delta(0, call_id="call_1", arguments="{}")],
                    finish_reason="tool_calls",
                )
            ]
        )

        with self.assertRaisesRegex(ModelProtocolError, "缺少工具名称"):
            list(model.stream([], []))

    def test_conflicting_tool_call_ids_are_rejected(self) -> None:
        model, _completions = self.build_model(
            [
                _chunk(
                    tool_calls=[
                        _tool_delta(0, call_id="call_1", name="calculator")
                    ]
                ),
                _chunk(
                    tool_calls=[
                        _tool_delta(0, call_id="call_2", arguments="{}")
                    ]
                ),
            ]
        )

        with self.assertRaisesRegex(ModelProtocolError, "不同的调用 ID"):
            list(model.stream([], []))

    def test_invalid_tool_index_is_reported_as_protocol_error(self) -> None:
        model, _completions = self.build_model(
            [_chunk(tool_calls=[_tool_delta(None, name="calculator")])]
        )

        with self.assertRaisesRegex(ModelProtocolError, "非负索引"):
            list(model.stream([], []))

    def test_abnormal_finish_reason_is_not_silently_accepted(self) -> None:
        model, _completions = self.build_model(
            [_chunk(content="未完成", finish_reason="length")]
        )
        events = iter(model.stream([], []))

        self.assertEqual(next(events), TextDelta("未完成"))
        with self.assertRaisesRegex(ModelProtocolError, "length"):
            next(events)

    def test_stream_interruption_is_propagated_after_emitted_text(self) -> None:
        def interrupted_chunks():
            yield _chunk(content="部分内容")
            raise RuntimeError("连接中断")

        model, _completions = self.build_model(interrupted_chunks())
        events = iter(model.stream([], []))

        self.assertEqual(next(events), TextDelta("部分内容"))
        with self.assertRaisesRegex(RuntimeError, "连接中断"):
            next(events)


if __name__ == "__main__":
    unittest.main()
