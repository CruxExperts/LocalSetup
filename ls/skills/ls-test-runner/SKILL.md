---
name: ls-test-runner
description: Use when writing or running tests across languages and frameworks (Vitest, Jest, pytest, XCTest, Playwright), including TDD workflow, coverage, and test patterns.
metadata:
  version: "1.1"
compatibility: "Examples assume the relevant local test runners are installed. Python ASGI API tests require pytest, pytest-asyncio, and httpx with ASGITransport support."
---

# test-runner

Write and run tests across languages and frameworks.

## Framework Selection

| Language | Unit Tests | Integration | E2E |
|----------|-----------|-------------|-----|
| TypeScript/JS | Vitest (preferred), Jest | Supertest | Playwright |
| Python | pytest | pytest + httpx | Playwright |
| Swift | XCTest | XCTest | XCUITest |

## Quick Start by Framework

### Vitest (TypeScript / JavaScript)
```bash
npm install -D vitest @testing-library/react @testing-library/jest-dom
```

```typescript
// vitest.config.ts - minimal configuration for non-DOM tests
import { defineConfig } from 'vitest/config'
export default defineConfig({
  test: {
    globals: true,
  },
})
```

Vitest uses the [Node environment by default](https://vitest.dev/guide/environment.html). For the React component example below or other DOM tests, retain the repository's configured DOM environment; select `environment: 'jsdom'` only when the target project already supplies a compatible `jsdom` dependency. Add [setupFiles](https://vitest.dev/config/setupfiles) only for an existing setup file with the required initialization and installed imports. The minimal configuration above does not create `tests/setup.ts` or provide a DOM environment.

```bash
npx vitest              # Watch mode
npx vitest run          # Single run
npx vitest --coverage   # With coverage
```

### Jest
```bash
npm install -D jest @types/jest ts-jest
```

```bash
npx jest                # Run all
npx jest --watch        # Watch mode
npx jest --coverage     # With coverage
npx jest path/to/test   # Single file
```

### pytest (Python)
```bash
uv add --dev pytest pytest-cov pytest-asyncio httpx
```

```bash
uv run pytest                   # Run all
uv run pytest -v                # Verbose
uv run pytest -x                # Stop on first failure
uv run pytest --cov=app         # With coverage
uv run pytest tests/test_api.py -k "test_login"  # Specific test
uv run pytest --tb=short        # Short tracebacks
```

### XCTest (Swift)
```bash
swift test                      # Run all tests
swift test --filter MyTests     # Specific test suite
swift test --parallel           # Parallel execution
```

### Playwright (E2E)
```bash
npm install -D @playwright/test
npx playwright install
```

```bash
npx playwright test                    # Run all
npx playwright test --headed           # With browser visible
npx playwright test --debug            # Debug mode
npx playwright test --project=chromium # Specific browser
npx playwright show-report             # View HTML report
```

## TDD Workflow

1. **Red** - Write a failing test that describes the desired behavior.
2. **Green** - Write the minimum code to make the test pass.
3. **Refactor** - Clean up the code while keeping tests green.

Legacy attribution: `test-runner` by cmanfre7. The tracked package does not retain the source URL, revision, digest, license, or cited release-only `_meta.json`; this attribution remains unverified.

## Test Patterns

### Arrange-Act-Assert
```typescript
test('calculates total with tax', () => {
  // Arrange
  const cart = new Cart([{ price: 100, qty: 2 }]);

  // Act
  const total = cart.totalWithTax(0.08);

  // Assert
  expect(total).toBe(216);
});
```

### Testing Async Code
```typescript
test('fetches user data', async () => {
  const user = await getUser('123');
  expect(user.name).toBe('Colt');
});
```

### Mocking
```typescript
import { vi } from 'vitest';

const mockFetch = vi.fn().mockResolvedValue({
  json: () => Promise.resolve({ id: 1, name: 'Test' }),
});
vi.stubGlobal('fetch', mockFetch);
```

### Testing API Endpoints (Python)
```python
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app

@pytest.mark.asyncio
async def test_get_users():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/users")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
```

### Testing React Components
```typescript
import { render, screen, fireEvent } from '@testing-library/react';
import { Button } from './Button';

test('calls onClick when clicked', () => {
  const handleClick = vi.fn();
  render(<Button onClick={handleClick}>Click me</Button>);
  fireEvent.click(screen.getByText('Click me'));
  expect(handleClick).toHaveBeenCalledOnce();
});
```

## Coverage Commands

```bash
# JavaScript/TypeScript
npx vitest --coverage          # Vitest (uses v8 or istanbul)
npx jest --coverage            # Jest

# Python
uv run pytest --cov=app --cov-report=html    # HTML report
uv run pytest --cov=app --cov-report=term    # Terminal output
uv run pytest --cov=app --cov-fail-under=80  # Fail if < 80%

# View HTML coverage report
open coverage/index.html       # macOS
open htmlcov/index.html        # Python
```

## What to Test

**Always test:**
- Public API / exported functions
- Edge cases: empty input, null, boundary values
- Error handling: invalid input, network failures
- Business logic: calculations, state transitions

**Don't bother testing:**
- Private implementation details
- Framework internals (React rendering, Express routing)
- Trivial getters/setters
- Third-party library behavior

## Vitest Planning Note

For Vitest work, inspect the repo's package manager, `vitest.config.*`, test environment, setup files, coverage provider, and UI/component test stack before adding commands. Prefer the repo's existing `npm test`, `npm run test`, or `npm run test:unit` scripts when present.

## Proportional validation and evidence reuse

Run inexpensive checks and focused tests while editing. One successful full
suite for the final candidate is sufficient; authoritative CI may provide it.
Do not run another local suite or repeat a successful suite for the same tested
inputs just because a task moves from PR to main or publication. Record the
commit, environment, command/workflow, and result; invalidate evidence only when
relevant inputs change. Bound long jobs, preserve successful jobs when retrying
an understood failure, and stop automatic retries on an unexplained failure.
Complete source changes and the release record before generating final docs.

LocalSetup's hosted implementation uses eight isolated Python shards with an
all-shards success gate and exact-commit Actions evidence. The publishing workflow
checks that evidence before building instead of running the full suite again.
See [repository maintenance](../../docs/REPO_MAINTENANCE.md#validation-reuse).
