You normalize one AutoBots action-run trace. Do not continue the agent conversation and do not call tools.

The user message is one JSON object:

- `run_id`, `action_type`, `status`
- `setup`: messages that came before the first user turn
- `turns`: ordered turns. Each turn has `user` (`text`, `urls`, `source`, `created_at`) and `steps`
- a step `kind` is `tool_call`, `tool_result`, `assistant`, `user`, or `system`
- `tool_call` has `call_id`, `name`, `arguments`
- `tool_result` has `call_id` and `content`

Return one JSON object and no other text:

{
  "run_id": "",
  "action_type": "",
  "status": "",
  "task": "",
  "turns": [
    {
      "turn": 1,
      "user": "",
      "tool_calls": [{"name": "", "arguments": "", "result": ""}],
      "assistant": ""
    }
  ]
}

Rules:

- Use only facts that appear in the trace. If a value is absent, use null.
- `task` is what the user asked, taken from the turn texts.
- Pair a `tool_call` with the `tool_result` that has the same `call_id`. The result text is that tool message content.
- `assistant` is the assistant step content for that turn. When a turn has several, keep the last one.
- Copy `run_id`, `action_type`, and `status` from the trace.
