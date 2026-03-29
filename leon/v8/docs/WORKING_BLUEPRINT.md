
# Working Blueprint

This project is being built as a multi-channel front-end AI form-guiding system.

## Core idea

- phone / SMS / web are the **front-end conversation layer**
- they know which field comes next and can fill a draft form
- the intake backend is the only authoritative source for session state, ticket validity, persistence, and submission
- OpenClaw is the orchestration brain behind the backend, not the public edge

## Main flow

Customer -> front-end conversation AI -> intake backend -> ticket store / policy -> OpenClaw -> business actions

## Key rules

1. Front end can propose. Backend decides.
2. Fixed prompts / prompt IDs are preferred for repeated voice steps.
3. Missed calls should create SMS follow-up actions.
4. OpenClaw receives sanitized, structured tickets through a dedicated low-privilege hook agent.
5. New requirements should update this blueprint and future implementation.
