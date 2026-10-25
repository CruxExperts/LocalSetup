---
status: ACTIVE
version: 4.84
owner_skill: ls-docs-create-move
---

# See the work. Keep control.

**LocalSetup is the harness for your agent harness.** It brings shared project
context, reusable capabilities, visible operations, and explicit control
boundaries to the AI tools you already use. Visibility, security, and privacy
are the organizing principles: understand what is happening, define what is
authorized, and make deliberate decisions about sensitive information.

Think of it as an engineering multi-tool built around those principles. Its
value comes from connecting useful capabilities through a consistent operating
layer: shared context, observable work, explicit authority, and evidence that
can be checked as tools and projects evolve.

An agent harness connects a model to tools and a working environment. LocalSetup
adds an operating layer around supported workflows: standards that survive a
conversation, procedures that can be reused, terminal work people can inspect,
and trust mechanisms for sensitive artifacts. Selected integrations make this
context available through the client's normal loading paths. The integration
should feel natural while remaining inspectable and explicitly configured.

## AI is a tool. Expertise determines what it multiplies.

AI can accelerate research, implementation, testing, and documentation. Its
usefulness still depends on understanding the problem, choosing appropriate
boundaries, and checking the result. Sound judgment becomes more productive;
poor assumptions can produce mistakes at greater speed.

LocalSetup's philosophy is practical. Define the outcome, observe the work,
test important assumptions, and retain evidence that someone else can inspect.
Use automation where it helps. Keep human judgment and responsibility clear.
Neither predictions of catastrophe nor promises of effortless transformation
help a business decide what to improve on Monday morning.

## Visibility: an operational view beside the conversation

A chat summary tells you what an agent says it did. A managed terminal companion
lets you inspect the operation itself. LocalSetup's tmux workflow gives commands
owned, persistent native panes with live output, progress, and session identity.
You can follow a long-running task, inspect retained output, and distinguish work
still running from a completed result without depending entirely on the agent's
narration.

This is the visibility side channel: a separate operator view of managed work.
It is not a secret data channel or an unrestricted terminal takeover. Observer
controls, qualified input handoffs, and ownership checks preserve the distinction
between watching an operation and interacting with it. Managed records support
reconciliation after uncertain outcomes; a dispatch acknowledgement is not proof
of completion.

The current native shell backend requires Linux and a qualified tmux environment.
It observes operations dispatched through that workflow, not every action of
every external AI client. See [the terminal workflow](TMUX_TERMINAL_MODE.md) for
the supported controls and prerequisites.

## Security: explicit authority and cryptographic trust

Security becomes useful when it is attached to a concrete operation. LocalSetup
provides shared OpenPGP capabilities for certificate inspection, full-fingerprint
trust, signed and encrypted envelopes, protected artifacts, and key lifecycle
workflows. Supported consumers bind trust to expected identities, recipients,
purposes, and scope rather than treating an incoming artifact as permission to
execute its contents.

Signing and encryption solve different problems. A verified signature supplies
evidence about origin and integrity under the selected trust policy. Encryption
protects selected content for its intended recipients. Neither independently
authorizes an operation. The surrounding workflow must still establish who may
act, on what target, and within which boundary.

The [shared OpenPGP runtime](OPENPGP_RUNTIME.md) is built into the framework's
capability set. Installation alone does not generate keys, enroll trust, or
activate consumers. OpenPGP protection applies to the workflows that use it;
ordinary project files, terminal output, and all model traffic are not thereby
encrypted. External clients retain their own permissions and security models.

## Privacy: choose what crosses each boundary

Useful automation needs context, but context should have an owner and a purpose.
LocalSetup separates reusable project guidance from private operational state,
provides protected secret-reference and input workflows, and keeps failure
evidence separate from reviewed publication payloads. The integrated
[LSCli runtime](LSCLI.md) adds explicit grants and brokered access within its
qualified execution paths.

These controls support deliberate disclosure. They do not make arbitrary
third-party skills safe or override another client's provider, logging, or
authentication behavior. A configured local workflow is not a promise that all
processing is offline. The [security policy](../../SECURITY.md) describes the
scope and reporting path; the relevant runtime documentation owns each boundary.

## A shared foundation for tools that keep changing

Switching clients should not require rebuilding every development convention.
LocalSetup organizes reviewable instructions, skills, workflows, and project
context into selected packages and supported adapters. Your architecture,
testing expectations, and operating practices become files you can maintain
beyond a single model session.

The benefit is less repeated setup and more reuse of tested procedures. It is
not a claim of permanent model memory, identical behavior across clients, or a
guaranteed reduction in token cost. Client loading rules still matter. The
optional integrated LSCli runtime is distinct from using the shared framework
with an existing coding assistant.

Configured [heartbeat automation](HARNESS_AUTOMATION.md) can also collect
repository evidence or run an explicitly enabled review outside the current
conversation. Collection is the default; scheduled agent review and installation
of a live schedule are separate choices. This is independent execution of an
approved check, not an always-on security service or a replacement sandbox.

## Engineering experience, multiplied by AI

Crux Experts' founder brings 35 years of hands-on IT and engineering experience.
That history includes building HTML websites for Netscape-era browsers in 1995,
working with fiber and Ethernet, network protocols and stacks, servers and data
centers, and programming with object-oriented design and general algorithms.

It also includes more than nine years owning and operating a web hosting company
before selling it. The business grew from a bedroom operation into an office with
employees, bootstrapped and wholly owned by its founder. That experience adds a
business perspective to the technical one: limited resources, operational
responsibility, and the need to make improvements that justify their cost.

AI is a force multiplier for that experience. It increases the reach of research
and engineering work while experienced judgment remains responsible for problem
selection, design, integration, and acceptance. LocalSetup reflects this
combination of systems knowledge, practical ownership, and continuously refined
automation.

## Practical service for small businesses

A small business needs a result it can use. The service begins with understanding
how the business works: where time is spent, which handoffs fail, what information
is duplicated, and which repetitive tasks create avoidable cost or errors.
Automation is one possible intervention, not the starting assumption.

A useful engagement follows a simple progression:

1. **Understand the work.** Map a real workflow and its constraints with the
   people who perform it.
2. **Choose a worthwhile improvement.** Identify a manageable opportunity and
   record a baseline such as handling time, rework, failure rate, or operating
   cost.
3. **Integrate it properly.** Fit the solution to existing systems, permissions,
   data boundaries, and staff responsibilities.
4. **Measure and maintain it.** Compare the result with the baseline, document
   what changed, and keep the integration useful as its dependencies evolve.

Possible starting points include document handling, recurring reporting,
information retrieval, repetitive administrative work, and infrastructure
checks. The right choice depends on the business. Savings are demonstrated
through measurement, not asserted through a universal percentage or a promise
that every task should use AI.

LocalSetup is the supporting system. The service is the expertise to recognize
an appropriate opportunity, deliver it responsibly, and sustain its value.

## The value continues after the first installation

Reviewable source is a foundation for trust and adaptation. Continued value comes
from the engineering around it: researching changes, testing integrations,
updating procedures, fixing failures, and understanding the customer's operating
environment. Tools, model behavior, APIs, and business requirements evolve. An
integration needs an owner throughout that evolution.

Crux Experts' proposition combines a maintained framework with experienced
implementation and support. Customers can inspect the system and discuss their
requirements with people responsible for the engineering. That ongoing
capability is the commercial value—not a claim that a snapshot of the code has a
predictable expiry date.

Current first-party source is free for noncommercial use under its stated terms.
Commercial licensing is available from [Crux Experts](https://www.cruxexperts.com/).
See [licensing](LICENSING.md) for scope and retained third-party and historical
rights. Detailed commercial offers belong on the company website and in the
applicable agreement.

## Explore the implementation

| Capability | Owning reference |
|---|---|
| Visible native operations and protected observation | [Terminal mode](TMUX_TERMINAL_MODE.md) |
| OpenPGP trust, signatures, encryption, and activation boundaries | [OpenPGP runtime](OPENPGP_RUNTIME.md) |
| Granted integrated agent execution | [LSCli](LSCLI.md) |
| Selected context and capability packages | [Framework architecture](FRAMEWORK_LIBRARY_ARCHITECTURE.md) |
| Opt-in periodic collection and review | [Harness automation](HARNESS_AUTOMATION.md) |
| Bounded tests and private failure feedback | [Repository maintenance](REPO_MAINTENANCE.md) |
| Supported capabilities and clients | [Features](FEATURES.md) and [platform registry](PLATFORM_REGISTRY.md) |

These references describe implemented scope. The narrative does not expand a
client's permissions, authorize background activity, or substitute a marketing
claim for operational evidence.
