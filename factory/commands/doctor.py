"""factory doctor: check everything `factory setup` and `factory start` need, print the fix
for each problem, and fail if anything blocks setup. Changes nothing itself.

Port of cmd_doctor in bin/factory, with the output unchanged: the same steps, styles,
indented fix text, problem count and exit status. `factory setup` runs it first.
"""

import os
import re

from factory import policy, preflight, roles, sbx
from factory.context import FactoryError

# A read-only lookup of a name nothing stores, to see whether secret-tool can answer.
SECRET_TOOL_PROBE = ["secret-tool", "lookup", "service", "factory-doctor-probe"]
SECRET_TOOL_TIMEOUT = 10

# The gateway's own doctor (broker/passdesk/doctor.py) runs inside the broker sandbox,
# where the baked-in policy, the state dir and the credential files are.
GATEWAY_DOCTOR = "cd /opt/factory-broker && PYTHONDONTWRITEBYTECODE=1 python3 -m passdesk.doctor"

_SPACE = " \t\n\v\f\r"


def secret_tool_probe(ctx):
    """Why secret-tool can't answer ("" when it can). A working one says "not found" with
    exit 1 and no message; one with no D-Bus session or no keyring service prints why."""
    result = ctx.capture(SECRET_TOOL_PROBE, timeout=SECRET_TOOL_TIMEOUT)
    if result.status == 124:
        return "timed out after %d seconds (is a keyring prompt waiting?)" % SECRET_TOOL_TIMEOUT
    return result.err.rstrip("\n")


class Doctor:
    def __init__(self, ctx):
        self.ctx = ctx
        self.paths = ctx.paths
        self.problems = 0
        self.linux = ctx.uname[0] == "Linux"
        self.wsl = preflight.is_wsl(ctx.host_files["proc_version"])
        self.roles_ready = False

    def fail(self, summary, *fixes):
        """A problem, with its fix indented beneath (a fix may span lines)."""
        self.ctx.bad(summary)
        for fix in fixes:
            if fix != "":
                for line in fix.split("\n"):
                    self.ctx.say("      " + line)
        self.problems += 1

    def fail_text(self, text):
        """A problem described by a check's text: a summary line, then what to do."""
        head, _, rest = text.rstrip("\n").partition("\n")
        self.fail(head, rest)

    def platform(self):
        ctx = self.ctx
        ctx.step("Platform")
        system, machine = ctx.uname
        if (system, machine) == ("Darwin", "arm64"):
            version = ctx.capture(["sw_vers", "-productVersion"]).out.rstrip("\n")
            major = version.split(".")[0].strip(_SPACE)
            if re.fullmatch("[0-9]+", major) and int(major) >= 14:
                ctx.ok("macOS %s, Apple silicon" % version)
            else:
                self.fail("macOS 14 (Sonoma) or later required")
        elif (system, machine) == ("Linux", "x86_64"):
            try:
                preflight.sbx_deb_pin(ctx.host_files["os_release"], self.wsl)
                ctx.ok(preflight.os_release_field("PRETTY_NAME", ctx.host_files["os_release"])
                       + (" (WSL2)" if self.wsl else ""))
            except preflight.Problem as e:
                self.fail_text(str(e))
            try:
                preflight.kvm_check(ctx.host_files["kvm"], self.wsl)
                ctx.ok("KVM available (/dev/kvm)")
            except preflight.Problem as e:
                self.fail_text(str(e))
        else:
            self.fail("unsupported platform %s/%s: macOS on Apple silicon or Ubuntu 24.04+ x86_64 "
                      "(including WSL2) only" % (system, machine))

    def tools(self):
        ctx = self.ctx
        ctx.step("Tools")
        missing = ""
        for tool, package in (("curl", "curl"), ("jq", "jq"), ("openssl", "openssl"), ("git", "git"),
                              ("column", "bsdextrautils")):
            if preflight.which(ctx, tool):
                ctx.ok(tool)
            else:
                ctx.bad("%s not found" % tool)
                missing += " " + package
                self.problems += 1
        try:
            ctx.ok("python3 (%s)" % preflight.resolve_python(ctx))
        except FactoryError as e:
            ctx.bad(str(e))
            if self.linux:
                missing += " python3"
            self.problems += 1
        if missing:
            if self.linux:
                ctx.say("      Install them: sudo apt-get update && sudo apt-get install -y" + missing)
            else:
                ctx.say("      Install them with Homebrew: brew install" + missing.replace("bsdextrautils", "", 1))

    def credential_store(self):
        ctx = self.ctx
        if preflight.keychain_backend(ctx) != "secret-tool":
            return
        ctx.step("Credential store (secret-tool)")
        why = secret_tool_probe(ctx)
        if not why:
            ctx.ok("secret-tool answers")
            return
        ctx.warn("secret-tool is installed but can't reach a keyring: " + why)
        ctx.say("      'factory secret save' will fail until it can. Start a keyring (e.g. gnome-keyring) in a desktop")
        ctx.say("      session, or skip the keychain: 'factory secret set <name>' delivers a credential without one.")

    def gateway_source(self):
        ctx, root = self.ctx, self.paths.root
        ctx.step("Gateway source (broker/)")
        if os.path.isfile(os.path.join(root, "broker", "tools.txt")):
            ctx.ok("broker/ is present")
            self.roles_ready = True
        else:
            self.fail("%s/broker/tools.txt is missing: broker/ is incomplete in this checkout" % root,
                      "restore it from the committed tree:", "  git -C '%s' checkout HEAD -- broker" % root)

    def docker(self):
        ctx = self.ctx
        ctx.step("Docker (builds the sandbox images)")
        if not preflight.which(ctx, "docker"):
            if self.linux:
                self.fail("docker not found", "Install Docker Engine in this Linux system (docs/setup-wsl.md, step 3):",
                          "  curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker $USER",
                          "then restart the shell (under WSL: 'wsl --shutdown' in PowerShell, reopen the terminal).")
            else:
                self.fail("docker not found", "Install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/")
            return
        info = ctx.capture(["docker", "info"])
        if info.status != 0:
            if "permission denied" in info.err:
                self.fail("docker is installed but %s can't use it" % preflight.user_name(),
                          "  sudo usermod -aG docker $USER",
                          "then restart the shell%s." % (" (under WSL: 'wsl --shutdown' in PowerShell)" if self.wsl else ""))
            elif self.wsl and ctx.capture(["ps", "-p", "1", "-o", "comm="]).out.rstrip("\n") != "systemd":
                self.fail("the Docker daemon isn't running, and WSL started without systemd",
                          "Add to /etc/wsl.conf (sudo nano /etc/wsl.conf):", "  [boot]", "  systemd=true",
                          "then run 'wsl --shutdown' in PowerShell and reopen the terminal.")
            elif self.linux:
                self.fail("the Docker daemon isn't running", "  sudo systemctl enable --now docker")
            else:
                self.fail("the Docker daemon isn't running", "Start Docker Desktop.")
            return
        ctx.ok("docker " + ctx.capture(["docker", "version", "--format", "{{.Server.Version}}"]).out.rstrip("\n"))
        if ctx.capture(["docker", "buildx", "version"]).status == 0:
            ctx.ok("docker buildx")
        else:
            self.fail("docker buildx not found (the broker image build needs it)",
                      "  sudo apt-get install -y docker-buildx-plugin" if self.linux else "Update Docker Desktop.")

    def factory_tools(self):
        ctx = self.ctx
        ctx.step("Factory tools")
        if self.sbx_ready:
            ctx.ok("sbx " + sbx.SBX_VERSION)
        else:
            ctx.warn("sbx %s not installed yet ('factory setup' installs it)" % sbx.SBX_VERSION)
        if self.sbx_ready:
            if sbx.setup_done(ctx):
                ctx.ok("setup done (security baseline applied, images loaded)")
            else:
                ctx.warn("setup not finished: run 'factory setup'")
            if sbx.claude_login_stored(ctx):
                ctx.ok("Claude login stored")
            else:
                ctx.warn("no Claude login yet: 'factory start' walks you through it")
        if self.wsl:
            d, n = ctx.style.D, ctx.style.N
            ctx.say("  %sKeep projects in the Linux filesystem (e.g. ~/code), not under /mnt/c: sandboxes mount%s" % (d, n))
            ctx.say("  %sthe project folder, and Windows drives are slow and lose file permissions there.%s" % (d, n))

    def gateway(self):
        """The gateway's own checks run in the broker sandbox. Its hosts-vs-kit check needs the
        kit files, which live on the host, so that comparison happens here."""
        ctx = self.ctx
        ctx.step("Passdesk (the gateway)")
        if not self.sbx_ready:
            ctx.warn("checked once sbx is installed and the broker sandbox exists ('factory setup', then 'factory start')")
            return
        if not sbx.sandbox_exists(ctx, sbx.BROKER_SANDBOX):
            ctx.warn("broker sandbox not created yet ('factory start' creates it)")
            return
        if not sbx.sandbox_running(ctx, sbx.BROKER_SANDBOX):
            ctx.warn("broker sandbox is not running ('factory start' starts it); its checks are skipped")
            return
        live = ctx.capture(["sbx", "exec", sbx.BROKER_SANDBOX, "sh", "-c", GATEWAY_DOCTOR + " --live --no-color"], merge=True)
        if re.search("No module named .*passdesk", live.out, re.S):
            ctx.warn("the broker image predates the gateway doctor; run 'factory update' to rebuild it")
            return
        # Its section headers and findings, indented under this step; its own summary line
        # is dropped in favour of this command's.
        for line in live.out.split("\n"):
            if line == "" or "problem(s) found." in line or "No problems found." in line:
                continue
            line = re.sub("^  ", "    ", line, count=1)
            line = re.sub("^==> ", "    ==> ", line, count=1)
            ctx.say(line)
        if live.status == 0:
            ctx.ok("gateway checks passed")
        else:
            self.fail("the gateway reported problems (above)")
        # Every host the baked-in policy can reach must be in the broker kit(s).
        hosts = ctx.capture(["sbx", "exec", sbx.BROKER_SANDBOX, "sh", "-c", GATEWAY_DOCTOR + " --egress-hosts"]).out.split()
        local_kit = os.path.join(self.paths.config, "broker-kit", "spec.yaml")
        kit_hosts = set()
        for kit in (os.path.join(self.paths.root, "kits", "broker", "spec.yaml"), local_kit):  # the local kit is optional
            if os.path.isfile(kit):
                with open(kit, "rb") as f:
                    for line in f.read().decode("utf-8", "replace").split("\n"):
                        m = re.match("[%s]*-[%s]*\"?([^\"%s#]*)" % (_SPACE, _SPACE, _SPACE), line)
                        if m:
                            kit_hosts.add(m.group(1))
        missing = "".join(" " + h for h in hosts if not h.startswith("*.") and h not in kit_hosts)
        if not hosts:
            ctx.warn("could not list the policy's hosts; kit check skipped")
        elif not missing:
            ctx.ok("every host the policy can reach is in the broker kit")
        else:
            self.fail("in the broker policy but not in the broker kit:" + missing,
                      "add them to kits/broker/spec.yaml (or %s), then 'factory update'" % local_kit)

    def role_overrides(self):
        """This machine's role overrides, checked exactly as `factory start` checks them; no
        step at all without the folder."""
        ctx, paths = self.ctx, self.paths
        folder = os.path.join(paths.config, "roles")
        if not os.path.isdir(folder):
            return
        ctx.step("Role overrides (%s)" % folder)
        if not self.roles_ready:
            ctx.warn("skipped: needs broker/ (see above)")
            return
        try:
            roles.validate_role_overrides(paths)
            for role in roles.list_roles(paths):
                roles.validate_role(paths, role)
        except FactoryError as e:
            self.fail(str(e), "fix or remove the override, then 'factory start' applies it (docs/guides/04-customize.md)")
        else:
            ctx.ok("role overrides are valid")

    def optional_roles(self):
        """A malformed enabled-roles file is a problem; a sandbox left from when a role was on
        is only a note, with the command that removes it (never run automatically)."""
        ctx, paths = self.ctx, self.paths
        if not self.roles_ready:
            return
        enabled_file = roles.enabled_roles_file(paths)
        disabled = roles.disabled_role_sandboxes(paths, sbx.sandbox_names(ctx))
        if not (os.path.exists(enabled_file) or disabled):
            return
        ctx.step("Optional roles")
        try:
            roles.validate_enabled_roles(paths)
        except FactoryError as e:
            self.fail(str(e), "fix the file (%s): one role name per line" % enabled_file)
        else:
            if os.path.exists(enabled_file):
                ctx.ok("enabled-roles is valid (%s)" % " ".join(roles.enabled_roles_listed(paths)))
        for sandbox in disabled:
            role = sandbox[len("factory-"):]
            ctx.warn("%s exists but its role (%s) is disabled, so it is not started or used" % (sandbox, role))
            ctx.say("      To remove it:  sbx rm --force " + sandbox)
            ctx.say("      To use it again: add '%s' to %s, then 'factory start'" % (role, enabled_file))

    def approvals(self):
        """Warn (not fail) about any expired card with no decision that still matches it;
        nothing printed when there are none, or when holds aren't in use."""
        ctx, paths = self.ctx, self.paths
        if not os.path.isdir(os.path.join(paths.broker_state, "approvals", "pending")):
            return
        try:
            python = preflight.resolve_python(ctx)
        except FactoryError:
            return
        summary = ctx.capture([python, os.path.join(paths.root, "scripts", "approvals.py"), "--state",
                               paths.broker_state, "summary"])
        if summary.status != 0:
            return
        # "pending oldest_min expired_unhandled approved running made failed"
        fields = summary.out.split()
        try:
            expired = int(fields[2]) if len(fields) > 2 else 0
        except ValueError:
            return
        if expired > 0:
            ctx.warn("%d expired approval(s) with no decision: nothing was written for them; run 'factory approvals' "
                     "(the agent's next call of each stages a fresh card)" % expired)

    def gitlab(self):
        """A warning, not a problem (owner decision): the fix is a Pro re-copy or a policy
        edit, and `factory update` must stay usable meanwhile."""
        if self.roles_ready:
            why = policy.gitlab_mr_unheld(self.paths)
            if why:
                self.ctx.warn("GitLab merge requests open without approval: %s (see README: Enabling approvals)" % why)

    def sbx_reference(self):
        ctx = self.ctx
        ctx.step("sbx CLI reference (docs/reference/sbx-cli.md)")
        why = sbx.reference_problem(os.path.join(self.paths.root, "docs", "reference", "sbx-cli.md"))
        if why:
            ctx.warn(why)
        else:
            ctx.ok("matches sbx " + sbx.SBX_VERSION)

    def summary(self):
        ctx, st = self.ctx, self.ctx.style
        ctx.say("")
        if self.problems == 0:
            ctx.say("%sNo problems found.%s" % (st.G, st.N))
            return 0
        ctx.say("%s%d problem(s) found.%s Fix them, then run %sfactory doctor%s again."
                % (st.R, self.problems, st.N, st.B, st.N))
        return 1

    def run(self):
        self.platform()
        self.tools()
        self.credential_store()
        self.gateway_source()
        self.docker()
        self.sbx_ready = bool(preflight.which(self.ctx, "sbx")) and sbx.installed_version(self.ctx) == sbx.SBX_VERSION
        self.factory_tools()
        self.gateway()
        self.role_overrides()
        self.optional_roles()
        self.approvals()
        self.gitlab()
        self.sbx_reference()
        return self.summary()


def run(ctx, args):
    return Doctor(ctx).run()
