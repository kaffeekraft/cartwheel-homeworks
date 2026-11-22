# Interface comparison

My review interface is `analysis/review_app/`. I compared it with the reference interface in `analysis/server.py` and `analysis/ui/`. The run instructions are in `analysis/review_app/README.md`.

## Retained: margin notes

I kept the reference's two-column layout, with the conversation on the left and notes in a margin on the right. I select the text that shows the problem and type a free-form note. The note sits beside the step it refers to, and the selected text is stored as the quote. Open coding stays a matter of writing what I see, not filling in a form.

## Changed after inspecting traces: one timeline per session

In Langfuse I had to click through the spans one at a time. The reasoning was in the fourth span. Every LLM span repeated the system prompt and the full history. I also could not tell whether a trace had follow-up turns.

My interface shows each session as one scrolling timeline:

- Turns are grouped by `cartwheel.session_id`, in chronological order, ending with END OF SESSION. Each turn shows the user message, the narration, the tool calls with their arguments and results, and the final reply.
- The system prompt and the scenario are shown once per session, collapsed.
- Each message appears once.
- Narration is the model text between tool calls that the user never sees. It is shown in a separate, dimmed style from the final reply, because only the final reply reaches the user.

## Remaining limitation: labels can drift from Langfuse

Each Pass/Fail judgment is written to a local append-only file and to a Langfuse score. Langfuse applies score writes and deletions through a queue, which took about 1–3 minutes in testing. If I change a judgment quickly, for example a Fail on turn 1 and then a Pass on turn 3, the old score can stay on the wrong trace. The local file stays correct, but Langfuse disagrees with it until I run `uv run python -m analysis.review_app.labels --sync`. The interface does not detect this drift by itself.
