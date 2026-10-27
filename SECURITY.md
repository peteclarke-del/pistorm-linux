# Security policy

## Supported versions

| Version | Security fixes |
| --- | --- |
| Current `main` branch | Yes |
| Latest published release | Yes |
| Older tags and unmaintained branches | No |

## Reporting a vulnerability

pistorm-linux is a PiStorm configuration manager for Linux. Do not open a public issue for a suspected vulnerability. Use
GitHub's private
[Report a vulnerability](https://github.com/peteclarke-del/pistorm-linux/security/advisories/new)
workflow. If that workflow is unavailable, contact the repository owner
privately using the address on the maintainer's GitHub profile. Include the
affected version or commit, host distribution, a minimal reproduction using
files you may lawfully share, and sanitised logs.

You should receive an acknowledgement within five working days. Disclosure
timing will be coordinated with the reporter. These are response targets, not
a service-level agreement.

Reports are especially useful for command or argument injection through a
file or device name, unsafe parsing of untrusted images or archives, path
traversal, unintended host-file or device access, a destructive operation that
happens without confirmation, and vulnerable dependencies.

Use disposable media and devices for research, and do not test against
systems or data you do not own. This project does not operate a bug bounty.