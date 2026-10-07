# Security policy

## What this server can do on your machine

carmaker-mcp runs locally, with your user rights, and acts on what an MCP client asks for. It can start
simulations, start MATLAB and CarMaker processes, edit files inside the configured CarMaker project, change
MATLAB workspace variables and Simulink parameters, and (only if you enable it with `CM_ENABLE=tcl`) run Tcl
commands in the CarMaker GUI. It opens no network port, makes no network connections of its own and collects
no telemetry. The safeguards and their limits are described in [docs/safety.md](docs/safety.md).

Treat the agent that uses the server like a colleague with access to your project: connect it only to clients
and models you trust, and be careful with prompts and files from unknown sources, because text an agent reads
can try to steer it (prompt injection).

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting instead:
**Security** tab of the repository, **Report a vulnerability**. Include what you did, what happened, and the
versions of carmaker-mcp, CarMaker and MATLAB.

You can expect a first answer within about two weeks. This is a volunteer project without a guaranteed
response time.

In scope: ways to write or delete outside the project folder, to bypass the path guard or the idle check, to
run commands the tool descriptions do not announce, or to make a revert destroy data. Out of scope: what the
documented tools do by design, and vulnerabilities in CarMaker, MATLAB or their interfaces (report those to
IPG Automotive or MathWorks).

## Supported versions

Only the latest release receives fixes.
