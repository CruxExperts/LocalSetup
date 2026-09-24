---
status: ACTIVE
version: 5.6
owner_package: ls-workflow-spec-clarify-reverse
---

# Decision tree workflow (reverse prompt)

Workflow ID: `spec-clarify-reverse`

**Purpose:** AI prompts the user one question at a time with four options (A-D), preferred choice, and rationale. Used to build context before implementation only when the user explicitly invokes the decision-tree or reverse-prompt workflow.

## Principle

- Questions are **relevant to the topic**. Goal: **maximum impact context**.
- **One question at a time.** Never dump multiple questions in one turn.

## Format for each question

1. **Setup (optional):** Short statement on topic and why it matters.
2. **Question or decision:** One single question or decision point.
3. **Options:** Four plausible answers labeled **A**, **B**, **C**, **D**.
4. **Preferred option:** State which the AI prefers (e.g. "Preferred: **B**").
5. **Rationale:** One paragraph explaining why that choice is optimal.
6. **User response:** User may pick A/B/C/D, different option, or free-form; use all feedback as context.

## Number of questions

- **Default:** About **7-9 questions** per topic unless user specifies otherwise.
- **Order:** Most important first. User may set a limit (e.g. "no more than 4").

## Flow

- One topic at a time. For each question: output in format above -> wait for answer -> next question or (if done) use answers as context for the requested follow-up work.

## When to use

- User explicitly says: "decision tree", "decision tree workflow", "reverse prompt", "reverse prompt workflow", "run the decision tree", or equivalent.
- Do not activate from ordinary PRD/spec edits unless the user names this workflow.

## Checklist

- One question per turn only. Four options (A-D); preferred stated; rationale. Accept A/B/C/D or free-form; use all feedback.
