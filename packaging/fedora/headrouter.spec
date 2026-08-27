# RPM spec for Headrouter (built by packaging/fedora/build-rpm.sh via rpmbuild).
# Version and Arch are passed in with --define from the build script.
%define _unpackaged_files_terminate_build 0

Name:           headrouter
Version:        %{headrouter_version}
Release:        1%{?dist}
Summary:        Tray controller and gateway for the Headrouter LLM gateway
License:        MIT
URL:            https://github.com/teomurgi/headrouter

# The tray uses AppIndicator via the system GObject introspection bindings;
# PyInstaller cannot bundle those typelibs, so they are runtime dependencies.
Requires:       python3-gobject gtk3 libayatana-appindicator-gtk3

# Self-contained frozen binaries; nothing to build in %build.
BuildArch:      %{headrouter_arch}

%description
Headrouter is a stateless OpenAI-compatible LLM gateway with Headroom
context compression and multi-provider routing. This package installs a
system-tray controller (start/stop the gateway, open the admin UI) plus a
self-contained gateway server binary it manages.

The tray frontend uses AppIndicator and requires the GNOME "AppIndicator and
KStatusNotifierItem Support" shell extension (or a desktop with a native
indicator area) to be visible.

%install
# The staging tree is prepared by build-rpm.sh; copy it into the buildroot.
mkdir -p %{buildroot}
cp -a %{headrouter_stage}/. %{buildroot}/

%files
/usr/bin/headrouter-gateway
/usr/bin/headrouter-tray
/usr/share/applications/headrouter.desktop
/usr/share/icons/hicolor/256x256/apps/headrouter.png
/usr/share/headrouter/providers.example.json
/etc/xdg/autostart/headrouter.desktop

%changelog
