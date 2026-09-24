# Changelog

## 0.1.3 — 2026-09-24

Added an explicit direct-to-main delivery mode for GitHub sessions and API tasks. The application
performs a normal push after checking the agent's changes; the model still cannot run Git writes.
The default remains a feature branch and pull request.

## 0.1.2 — 2026-09-24

Added Responses API support to the built-in coding harness, including medium reasoning and
multi-step function calling. Chat Completions remains supported for existing model endpoints.

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
