# Changelog

## 0.1.1 — 2026-09-24

Validated the first Azure Container Apps deployment, added secretless GitHub OIDC rollout of the
published image digest, and made chat-completions reasoning effort configurable for models that
require `reasoning_effort=none` when using function tools.

## 0.1.0 — 2026-09-24

Initial public preview. Includes authenticated multi-repository sessions, isolated one-turn
Container Apps Jobs, Blob-backed conversation history, GitHub branch and pull-request delivery,
the private-model coding harness, optional upstream DeepSeek Harness and Copilot CLI adapters,
managed-identity authentication for Microsoft Foundry, and versioned GHCR images.

This 0.x release is intended for trusted repositories and users. It has not yet been validated
against a live Azure deployment; review the security model in the README before exposing it.
