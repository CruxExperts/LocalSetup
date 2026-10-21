# Testing Framework Guide

Language and framework selection, configuration, and patterns.

---

## Table of Contents

- [Framework Selection](#framework-selection)
- [TypeScript/JavaScript](#typescriptjavascript)
- [Python](#python)
- [Java](#java)
- [Compatibility Matrix](#compatibility-matrix)

---

## Framework Selection

| Language | Recommended | Alternatives | Best For |
|----------|-------------|--------------|----------|
| TypeScript/JS | Jest 30 | Vitest 5, Mocha, Jasmine | React, Node.js, Next.js |
| Python | pytest 9 | unittest | Django, Flask, FastAPI |
| Java | JUnit 6 | TestNG | Spring and JVM services |
| Vite projects | Vitest 5 | Jest 30 | Vite 6.4+ applications |

---

## TypeScript/JavaScript

### Jest Configuration

```javascript
// jest.config.js
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  testMatch: ['**/*.test.ts'],
  collectCoverageFrom: ['src/**/*.ts'],
  coverageThreshold: {
    global: { branches: 80, lines: 80 }
  }
};
```

### Jest Test Pattern

```typescript
describe('Calculator', () => {
  let calc: Calculator;

  beforeEach(() => {
    calc = new Calculator();
  });

  it('should add two numbers', () => {
    expect(calc.add(2, 3)).toBe(5);
  });

  it('should throw on invalid input', () => {
    expect(() => calc.add(null, 3)).toThrow('Invalid input');
  });
});
```

### Vitest Configuration

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    coverage: { provider: 'v8' }
  }
});
```

### Coverage Tools
- Istanbul/nyc: Traditional coverage
- c8: Native V8 coverage (faster)
- Vitest built-in: Integrated with test runner

---

## Python

### Pytest Configuration

```ini
# pytest.ini
[pytest]
testpaths = tests
python_files = test_*.py
python_functions = test_*
addopts = --cov=src --cov-report=term-missing
```

### Pytest Test Pattern

```python
import pytest
from calculator import Calculator

class TestCalculator:
    @pytest.fixture
    def calc(self):
        return Calculator()

    def test_add_positive_numbers(self, calc):
        assert calc.add(2, 3) == 5

    def test_add_raises_on_invalid_input(self, calc):
        with pytest.raises(ValueError, match="Invalid input"):
            calc.add(None, 3)

    @pytest.mark.parametrize("a,b,expected", [
        (1, 2, 3),
        (-1, 1, 0),
        (0, 0, 0),
    ])
    def test_add_various_inputs(self, calc, a, b, expected):
        assert calc.add(a, b) == expected
```

### Coverage Tools
- coverage.py: Standard Python coverage
- pytest-cov: Pytest plugin wrapper
- Report formats: HTML, XML, LCOV

---

## Java

### JUnit 6 Configuration (Maven)

```xml
<!-- pom.xml -->
<dependency>
    <groupId>org.junit.jupiter</groupId>
    <artifactId>junit-jupiter</artifactId>
    <version>6.0.0</version>
    <scope>test</scope>
</dependency>
<plugin>
    <groupId>org.jacoco</groupId>
    <artifactId>jacoco-maven-plugin</artifactId>
    <version>0.8.10</version>
</plugin>
```

### JUnit 6 Test Pattern

```java
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import static org.junit.jupiter.api.Assertions.*;

class CalculatorTest {
    private Calculator calc;

    @BeforeEach
    void setUp() {
        calc = new Calculator();
    }

    @Test
    @DisplayName("should add two positive numbers")
    void testAddPositive() {
        assertEquals(5, calc.add(2, 3));
    }

    @Test
    @DisplayName("should throw on null input")
    void testAddThrowsOnNull() {
        assertThrows(IllegalArgumentException.class,
            () -> calc.add(null, 3));
    }

    @ParameterizedTest
    @CsvSource({"1,2,3", "-1,1,0", "0,0,0"})
    void testAddVarious(int a, int b, int expected) {
        assertEquals(expected, calc.add(a, b));
    }
}
```

### Coverage Tools
- JaCoCo: Standard Java coverage
- Cobertura: Alternative XML format
- Report formats: HTML, XML, CSV

---

## Compatibility Matrix

This is the package's only authoritative framework matrix. The shared Node floor is deliberately the stricter Vitest floor so every advertised Node framework works in one environment.

| Runtime or tool | Supported floor | Compatibility reason |
|-----------------|-----------------|----------------------|
| Node.js | 22.12+ | Vitest 5 requires Node 22.12+; this also satisfies Jest 30's Node 18+ floor |
| Jest | 30+ | Current supported Jest major represented by the generated API |
| Vitest | 5+ | Current Vitest major; pair with Vite 6.4+ |
| Vite | 6.4+ | Minimum accepted by Vitest 5 |
| Python | 3.12+ | LocalSetup package runtime floor |
| pytest | 9+ | Current pytest major; its upstream Python 3.10+ floor is covered by Python 3.12+ |
| Java | 17+ | Minimum runtime for JUnit 6 |
| JUnit | 6+ | Current JUnit generation and examples |
| TypeScript | 5.4+ | Baseline for current typed examples |

Verified against primary upstream documentation on 2026-09-02:

- [Node.js release schedule](https://nodejs.org/en/about/previous-releases)
- [Jest 30 upgrade guide](https://jestjs.io/docs/upgrading-to-jest30)
- [Vitest guide](https://main.vitest.dev/guide/)
- [pytest 9 documentation](https://docs.pytest.org/en/9.0.x/)
- [Python 3.12 release](https://www.python.org/downloads/release/python-3120/)
- [JUnit current user guide](https://docs.junit.org/current/user-guide/)
- [TypeScript 5.4 release notes](https://www.typescriptlang.org/docs/handbook/release-notes/typescript-5-4.html)
