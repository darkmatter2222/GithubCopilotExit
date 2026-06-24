# Ollama Issue #10976 — Comprehensive Root Cause Analysis & Related Issues Catalog

**Anchor Issue:** https://github.com/ollama/ollama/issues/10976  
**Proposed Fix:** https://github.com/ollama/ollama/pull/16758  
**Date:** 2026-06-16  
**Scope:** All issues sharing the same root cause in Ollama's ChatHandler thinking promotion logic  

---

## EXECUTIVE SUMMARY

A single policy conflict in `server/routes.go:ChatHandler` is responsible for **at least 15 distinct GitHub issues** affecting Qwen3, Qwen3.5, Qwen3.6, Gemma4, and qwen3-coder models. The root cause: when a thinking-capable model receives tools via the OpenAI-compatible `/v1/chat/completions` endpoint, Ollama unconditionally promotes `req.Think=nil` → `Think=true`. This causes the model to emit all output inside `<thinking>` blocks with zero visible content and no tool calls.

**Impact:** Every OpenAI-compatible client (VS Code Copilot, agent frameworks, Claude Desktop, etc.) that sends tools but never sends a thinking preference is broken by default.

**Proposed Fix (PR #16758):** Add `defaultChatThink(hasTools)` helper — when tools present + no explicit think preference, default to `think=false` instead of `true`.

---

## TABLE OF CONTENTS

1. [Core Root Cause: ChatHandler Think Promotion](#1-core-root-cause)
2. [PRIMARY CLUSTER — Thinking + Tools = Empty Output (100% Same Root Cause)](#2-primary-cluster)
3. [PARSER TOOL CALL DROPS (85-90% Related — Parser-Side Manifestation)](#3-parser-tool-call-drops)
4. [STRUCTURED OUTPUT + THINKING CONFLICTS (75-85% Related — Format Masking Deferral Bug)](#4-structured-output-conflicts)
5. [GEMMA4-SPECIFIC ISSUES (60-70% Related — Same Deferral Pattern, Different Model)](#5-gemma4-specific)
6. [RENDERER/PARSER FORMAT MISMATCHES (50-65% Related — Exacerbated by Primary Bug)](#6-renderer-mismatches)
7. [Workaround Analysis — Why reasoning_effort="none" Is the Only Fix Today](#7-workaround-analysis)
8. [Merged/Superseded PRs That Partially Addressed Symptoms](#8-merged-prs)
9. [FALSE POSITIVES — Issues Initially Tracked But Unrelated](#9-false-positives)
10. [Complete Issue Matrix with Relevance Scores](#10-complete-matrix)
11. [Proposed Fix Detail — PR #16758 Breakdown](#11-proposed-fix-detail)

---

## 1. CORE ROOT CAUSE

### File: `server/routes.go` — ChatHandler

```go
// Lines ~2100-2150 (Ollama 0.30.6)
caps := []model.Capability{model.CapabilityCompletion}
if len(req.Tools) > 0 {
    caps = append(caps, model.CapabilityTools)
}
modelCaps := m.Capabilities()
if slices.Contains(modelCaps, model.CapabilityThinking) {
    caps = append(caps, model.CapabilityThinking)
    if req.Think == nil {
        req.Think = &api.ThinkValue{Value: true}   // ← THE BUG: FORCED ON when nil
    }
}
```

### The 5-Step Failure Chain (Confirmed by @darkmatter2222 code-level trace)

| Step | Location | What Happens | Result |
|------|----------|-------------|--------|
| 1 | `openai/openai.go:FromChatRequest()` | VS Code Copilot sends tools + no reasoning_effort → Think=nil | Nil enters ChatHandler |
| 2 | `server/routes.go:ChatHandler()` | Model has CapabilityThinking → Think forced to true | Thinking ON |
| 3 | `server/routes.go` — thinking state machine | Both thinkingState and toolParser created simultaneously | Two competing parsers |
| 4 | Streaming callback | Thinking parser strips `<thinking>` content first, leaving empty string for tool parser | Tool parser sees "" |
| 5 | Model generation | Qwen3 puts ALL output in `<thinking>` block (892 tokens), never emits `