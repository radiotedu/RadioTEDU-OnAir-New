RadioTEDU Services Companion
============================

This deliberately contains only JukeLocal, Voting, and the AI host controls.
It contains no playout, encoder, Icecast source, mount control, or streaming code.

RadioTEDU-Services.exe reads connections.json from the same folder. Edit the
streaming_pc LAN address after moving the companion to another PC.

The existing RadioTEDU Windows services remain the process owner and restart
authority. Run this application as Administrator to Start/Stop/Restart them.
Secrets are not copied into this portable folder. Keep each service .env file
in C:\ProgramData\RadioTEDU\OnAir\secrets on the destination PC.

Run Install-On-Another-PC.ps1 once on the destination PC. It installs missing
Node/Python/Ollama runtimes, dependencies, resilient Windows services, and a
desktop shortcut. It does not install or start any streaming component.
