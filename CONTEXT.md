# Omnigent Context

This context defines the user-facing containers and defaults that organize Omnigent sessions.

## Language

**Profile**:
A switchable personal context that owns projects and sessions and provides baseline defaults and presentation settings.
_Avoid_: Workspace, persona, account

**Active profile**:
The profile currently selected on one client. Selection is client-local so different devices can show different profiles for the same user.
_Avoid_: Current workspace, global profile

**Project**:
An owner-private group of related sessions within exactly one profile. A project may override its profile's session defaults.
_Avoid_: Folder, workspace

**Profile defaults**:
Baseline, user-overridable values used when creating a session, such as execution host, working directory, and agent.
_Avoid_: Policy, requirements

**Project defaults**:
User-overridable session-creation values layered over profile defaults for one project.
_Avoid_: Project policy, enforced settings

**Private profile**:
A profile configured with explicit protection controls for disclosure and access. “Private” is a preset over those controls, not a claim of cryptographic storage isolation.
_Avoid_: Secret profile, secret project

Private profiles have one or more canonical protected roots and a per-profile passcode. Locked clients can see that the profile exists, but not its configured paths, projects, sessions, or activity. Browser unlock bearers live only in page memory. Android may encrypt the passcode with an Android Keystore key that requires biometric authentication for every use.

On local Linux, protected roots are a server-wide isolation floor. A runner in one private profile keeps that profile's roots while bubblewrap masks every peer profile's roots. A runner outside all private roots gets every private root masked and fails to launch if its effective sandbox is not `linux_bwrap`, even when its ordinary access policy is otherwise unrestricted. This is best-effort same-user isolation, not protection from root, `sudo`, ptrace, direct database access, or processes launched outside Omnigent.

**Shared session**:
A session owned by another user and granted to the viewer. Shared sessions remain reachable independently of the viewer's active profile.
_Avoid_: Shared profile
