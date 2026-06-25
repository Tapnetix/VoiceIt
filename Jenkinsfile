// VoiceIt cross-platform desktop build.
//
// Stages:
//   Verify — fast gates that block the long Build: bun typecheck, Vitest
//            (617 tests), pytest with the audit-coverage 80% gate, cargo
//            test. ~8 min wall, all on a Linux agent. Any failure short-
//            circuits the build.
//   Build  — Linux .deb, macOS .dmg, Windows NSIS .exe in parallel. Bundles
//            are large (~2.7 GB — the PyInstaller sidecar packs torch + the
//            TTS engines), so one installer type per platform.
//
// Updater artifacts are disabled for CI (scripts/ci-disable-updater.py): the
// conf pins an updater pubkey + createUpdaterArtifacts, which otherwise makes
// tauri block on a signing-key password prompt and error when the bundle target
// isn't updater-enabled (e.g. .deb).
//
// Agent toolchains:
//   Linux  `pockeo-linux`   — /home/jenkins toolchains; `just` + python3 present.
//   macOS  `macos` (mbook)  — runs as jenkins; force HOME=/Users/jenkins; Python
//                              3.12 via uv (brew unwritable); bun in workspace home.
//                              Installs MLX (Apple-Silicon accel) + ad-hoc signs the
//                              .dmg so Gatekeeper shows the normal trust prompt.
//   Windows `pockeo-windows`— cmd/`bat` (the `powershell` step isn't on PATH there);
//                              uv + bun pre-provisioned; Python 3.12 via uv.

pipeline {
    agent none

    options {
        timestamps()
        timeout(time: 180, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '15'))
        disableConcurrentBuilds()
    }

    environment {
        CARGO_TERM_COLOR = 'always'
        RUST_BACKTRACE = '1'
        CI = 'true'
    }

    stages {

        // ─── Verify: fast gates before the long Build ──────────────────
        // Frontend (bun typecheck + Vitest + build:web smoke), backend
        // (pytest with --cov-config=backend/pyproject.toml + 80% gate),
        // rust (`cargo test --lib --tests`). All three on the Linux pool;
        // they run in parallel so the bottleneck is whichever is slowest
        // (pytest, ~6 min). Any failure short-circuits Build via failFast.
        stage('Verify') {
            failFast true
            parallel {

                stage('Verify: frontend') {
                    agent { label 'pockeo-linux' }
                    steps {
                        sh '''
                            set -eu
                            export PATH="$HOME/.bun/bin:$PATH"
                            command -v bun >/dev/null 2>&1 || curl -fsSL https://bun.sh/install | bash
                            export PATH="$HOME/.bun/bin:$PATH"
                            bun --version
                            bun install --frozen-lockfile
                            # Typecheck app + web workspaces (this is what the GH
                            # `ci.yml` ran; replaces it as the fast PR-style gate).
                            bun run --filter '*' typecheck
                            # Vitest — 617 tests, ~9s. Coverage measured but not
                            # gated here (the audit harness already enforced 80% on
                            # the post-merge HEAD; CI re-runs the suite without the
                            # 30s coverage instrumentation overhead).
                            ( cd app && bun x vitest run --reporter=default )
                            # Web smoke build — catches build-config regressions
                            # before the heavy Tauri bundle in Build.
                            bun run build:web
                        '''
                    }
                }

                stage('Verify: backend') {
                    agent { label 'pockeo-linux' }
                    steps {
                        sh '''
                            set -eu
                            export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
                            command -v just >/dev/null 2>&1 || cargo install just --locked
                            python3 --version
                            # backend/services/audiobook_export.py shells out to
                            # ffmpeg/ffprobe; the pockeo-linux agent doesn't ship
                            # them. Drop a static build into $HOME/.local/bin
                            # (idempotent — keeps the binary across builds for a
                            # ~30 MB one-time download).
                            if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
                                mkdir -p "$HOME/.local/bin"
                                TMP=$(mktemp -d)
                                curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
                                    -o "$TMP/ff.tar.xz"
                                tar -xJf "$TMP/ff.tar.xz" -C "$TMP"
                                cp "$TMP"/ffmpeg-*-amd64-static/ffmpeg "$TMP"/ffmpeg-*-amd64-static/ffprobe "$HOME/.local/bin/"
                                rm -rf "$TMP"
                            fi
                            ffmpeg -version | head -1
                            ffprobe -version | head -1
                            # Recreate the venv from scratch — see the Linux Build
                            # stage's note about stale shebangs after workspace
                            # renames.
                            rm -rf backend/venv
                            just setup-python
                            # `just setup-python` chains through `_ensure-venv`,
                            # which installs pytest + pytest-asyncio + pytest-cov.
                            # Add the test-only dep mutagen (used by
                            # test_audiobook_export.py to read ID3 tags off the
                            # generated .mp3s) — it isn't in requirements.txt
                            # because no production code imports it.
                            backend/venv/bin/pip install --quiet mutagen
                            # Run with the audit omit list applied (--cov-config is
                            # mandatory; without it pytest-cov silently ignores the
                            # config under [tool.coverage.run] when invoked from
                            # the repo root).
                            backend/venv/bin/python -m pytest backend/tests \\
                                --cov=backend \\
                                --cov-config=backend/pyproject.toml \\
                                --cov-report=term \\
                                --cov-report=json:py-cov.json \\
                                -q
                            # 80% gate — matches the audit-coverage design target.
                            backend/venv/bin/python - <<'PYGATE'
import json, sys
data = json.load(open('py-cov.json'))
pct = data['totals']['percent_covered']
print(f"Python coverage: {pct:.2f}% (gate: 80%)")
sys.exit(0 if pct >= 80 else 1)
PYGATE
                        '''
                    }
                }

                stage('Verify: rust') {
                    agent { label 'pockeo-linux' }
                    steps {
                        sh '''
                            set -eu
                            export PATH="$HOME/.cargo/bin:$PATH"
                            rustc --version; cargo --version
                            # tauri-build's resource-validation pass refuses to
                            # link without the declared sidecar binaries present.
                            # Place empty stubs so `cargo test` can build the lib +
                            # integration tests (the real binaries land later in
                            # the Build stage). These stubs are gitignored.
                            cd tauri/src-tauri
                            mkdir -p binaries
                            touch binaries/voiceit-server-x86_64-unknown-linux-gnu \\
                                  binaries/voiceit-mcp-x86_64-unknown-linux-gnu
                            # `--skip test_system_audio_capture` — that test opens
                            # a real audio device and panics on headless Linux.
                            # Gated behind #[ignore] post-F5, but skipping here
                            # keeps the run portable.
                            cargo test --lib --tests -- --skip test_system_audio_capture
                            # Coverage gate via cargo-tarpaulin is intentionally
                            # NOT run here: bootstrapping tarpaulin on a cold
                            # agent is 5–15 min; the audit closed the gap to 100%
                            # on pure-logic and `cargo test` catches regressions.
                            # If/when tarpaulin lands on the agent toolchain, add
                            # the pure-logic 60% gate here.
                        '''
                    }
                }
            }
        }

        // On tag pushes the Release stage below does the production bundling
        // with real signing + updater artifacts, so the regression-detection
        // Build stage is redundant — skip it to halve tag-build wall time.
        stage('Build') {
            when { not { buildingTag() } }
            failFast false
            parallel {

                // ─── Linux (.deb) ───────────────────────────────────────────
                stage('Linux') {
                    agent { label 'pockeo-linux' }
                    steps {
                        sh '''
                            set -eu
                            export PATH="$HOME/.cargo/bin:$HOME/.bun/bin:$HOME/.local/bin:$PATH"
                            command -v bun  >/dev/null 2>&1 || curl -fsSL https://bun.sh/install | bash
                            export PATH="$HOME/.bun/bin:$PATH"
                            command -v just >/dev/null 2>&1 || cargo install just --locked
                            rustc --version; bun --version; just --version; python3 --version

                            # Fail fast on a broken bundle icon config before the build (shared check;
                            # plain ESM — run with node if present, else bun).
                            if command -v node >/dev/null 2>&1; then node scripts/check-bundle-icons.mjs; else bun scripts/check-bundle-icons.mjs; fi

                            # Warm workspaces can carry a stale backend/venv whose interpreter
                            # shebang points at an old path (e.g. after a job/workspace rename),
                            # which breaks `just setup`'s setup-python with a bad-interpreter error.
                            # Recreate it from scratch (macOS stage already does this).
                            rm -rf backend/venv
                            # Drop stale Tauri ACL codegen: a bundle-identifier/config change can
                            # leave the warm target/ with mismatched autogenerated permission tomls
                            # ("failed to read plugin permissions … app_hide.toml"). Forcing the
                            # tauri build script to re-run regenerates them.
                            rm -rf tauri/src-tauri/target/*/build/tauri-* tauri/src-tauri/target/*/.fingerprint/tauri-* 2>/dev/null || true

                            just setup
                            # The default (CPU) sidecar must NOT ship the ~2.7GB CUDA/NVIDIA
                            # libs that plain `torch` pulls on Linux (torch x.y+cuNNN). Swap to
                            # CPU torch + drop orphaned nvidia-* wheels so the .deb is ~0.5GB
                            # not 2.5GB. (CUDA users get the separate voiceit-server-cuda.)
                            backend/venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu --force-reinstall torch torchaudio
                            backend/venv/bin/pip freeze | grep -iE '^nvidia[-_]' | cut -d'=' -f1 | xargs -r backend/venv/bin/pip uninstall -y || true
                            just build-server
                            python3 scripts/ci-disable-updater.py
                            ( cd tauri && bun run tauri build --bundles deb < /dev/null )
                        '''
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'tauri/src-tauri/target/release/bundle/deb/*.deb',
                                allowEmptyArchive: false, fingerprint: true
                        }
                        cleanup { sh 'rm -rf tauri/src-tauri/target/release/bundle || true' }
                    }
                }

                // ─── macOS (.dmg) ───────────────────────────────────────────
                stage('macOS') {
                    agent { label 'macos' }
                    steps {
                        sh '''
                            set -eu
                            export HOME=/Users/jenkins
                            export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/Users/jenkins/.nvm/versions/node/v24.14.1/bin:/Users/jenkins/.nvm/versions/node/v24.14.0/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
                            export BUN_INSTALL="$HOME/.bun"
                            command -v bun >/dev/null 2>&1 || curl -fsSL https://bun.sh/install | bash
                            export PATH="$BUN_INSTALL/bin:$PATH"
                            uv --version; rustc --version; bun --version

                            # Fail fast on a broken icon config BEFORE the long build (catches a
                            # malformed icns or a CFBundleIconFile that doesn't match the bundled icns).
                            node scripts/check-bundle-icons.mjs

                            rm -rf backend/venv
                            uv venv --python 3.12 --seed backend/venv
                            backend/venv/bin/python -m pip install --upgrade pip
                            backend/venv/bin/pip install -r backend/requirements.txt
                            # Apple Silicon acceleration: without MLX the sidecar falls back to the
                            # generic PyTorch path (LLM on MPS, TTS on CPU) and runs slowly even on
                            # M-series. build_binary.py already bundles MLX on arm64 (--collect-all
                            # mlx/mlx_audio/mlx_lm) — it just needs the packages present in the venv.
                            # mlx-lm / mlx-audio declare transformers>=5.x (conflicts with our 4.57.x
                            # cap) so install them --no-deps; mirrors .github/workflows/release.yml.
                            backend/venv/bin/pip install -r backend/requirements-mlx.txt
                            backend/venv/bin/pip install --no-deps mlx-lm==0.31.1
                            backend/venv/bin/pip install --no-deps mlx-audio==0.4.1
                            bun install
                            ./scripts/build-server.sh
                            backend/venv/bin/python scripts/ci-disable-updater.py
                            # Drop stale Tauri ACL codegen from the warm target/ (a bundle-identifier
                            # change otherwise leaves mismatched autogenerated permission tomls →
                            # "failed to read plugin permissions … app_hide.toml"). Forces regen.
                            rm -rf tauri/src-tauri/target/*/build/tauri-* tauri/src-tauri/target/*/.fingerprint/tauri-* 2>/dev/null || true

                            # Ad-hoc code signing ("-"). The app was previously shipped fully
                            # unsigned, so Gatekeeper flagged it as "damaged"/unverifiable instead of
                            # the normal "unidentified developer → right-click Open" flow. Tauri reads
                            # APPLE_SIGNING_IDENTITY and signs the .app at build time so the copy
                            # inside the .dmg is signed too.
                            export APPLE_SIGNING_IDENTITY="-"
                            ( cd tauri && bun run tauri build --bundles dmg < /dev/null )

                            # Tauri already signed the .app with the ad-hoc identity ("-") above and
                            # bundled it into its OWN styled .dmg — drag-to-Applications symlink,
                            # background, Finder window layout, and a blessed .VolumeIcon.icns. Ship
                            # that dmg AS-IS. Do NOT rebuild it with `hdiutil create -srcfolder`: that
                            # strips the layout/background and the volume-icon bless, producing the
                            # bare, icon-less image we used to emit. Verify the signature only.
                            # (For a fully trusted, no-warning install, set a real Developer ID +
                            # APPLE_ID/APPLE_PASSWORD/APPLE_TEAM_ID and notarize the Tauri dmg here.)
                            # ── Verify the produced dmg on the real artifact ──────────────
                            # Mounts the dmg and asserts: signed .app + a decodable app icon
                            # (CFBundleIconFile resolves to a Resources icns that `sips` can read)
                            # + a blessed .VolumeIcon.icns + the Applications drag symlink. This is
                            # the last line of defence against the icon/dmg regressions: a malformed
                            # icns or a bad CFBundleIconFile FAILS the build instead of shipping a
                            # blank-icon/bare dmg. (The pre-build check-bundle-icons.mjs gate catches
                            # the config statically; this confirms the actual packaged result.)
                            DMG=$(find tauri/src-tauri/target/release/bundle/dmg -name "*.dmg" | head -1 || true)
                            if [ -n "$DMG" ]; then
                                echo "=== Tauri styled dmg: $(du -h "$DMG" | cut -f1) ==="
                                MNT=$(mktemp -d); fail=0
                                hdiutil attach "$DMG" -mountpoint "$MNT" -nobrowse -quiet
                                APP=$(find "$MNT" -maxdepth 1 -name "*.app" -type d | head -1 || true)
                                [ -n "$APP" ] || { echo "VERIFY: no .app inside dmg"; fail=1; }
                                if [ -n "$APP" ]; then
                                    codesign --verify --deep --strict "$APP" && echo "VERIFY: signature OK" || { echo "VERIFY: signature INVALID"; fail=1; }
                                    ICONKEY=$(defaults read "$APP/Contents/Info" CFBundleIconFile 2>/dev/null || true)
                                    case "$ICONKEY" in
                                        icon|icon.icns) echo "VERIFY: CFBundleIconFile=$ICONKEY" ;;
                                        *) echo "VERIFY: CFBundleIconFile='$ICONKEY' is not the bundled icon.icns"; fail=1 ;;
                                    esac
                                    ICNS="$APP/Contents/Resources/${ICONKEY%.icns}.icns"
                                    if [ -f "$ICNS" ]; then
                                        W=$(sips -g pixelWidth "$ICNS" 2>/dev/null | awk '/pixelWidth/{print $2}')
                                        if [ -n "$W" ] && [ "$W" -ge 256 ]; then echo "VERIFY: app icon decodes (${W}px)"; else echo "VERIFY: app icns does not decode (macOS would show a blank icon)"; fail=1; fi
                                    else
                                        echo "VERIFY: missing Resources icns ($ICNS)"; fail=1
                                    fi
                                fi
                                [ -f "$MNT/.VolumeIcon.icns" ] && echo "VERIFY: .VolumeIcon.icns present" || { echo "VERIFY: no .VolumeIcon.icns (bare volume)"; fail=1; }
                                [ -L "$MNT/Applications" ] && echo "VERIFY: Applications symlink present" || { echo "VERIFY: no Applications drag symlink"; fail=1; }
                                hdiutil detach "$MNT" -quiet
                                if [ "$fail" = 0 ]; then echo "=== dmg verification: PASS ==="; else echo "=== dmg verification: FAIL ==="; exit 1; fi
                            fi
                        '''
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'tauri/src-tauri/target/release/bundle/dmg/*.dmg',
                                allowEmptyArchive: true, fingerprint: true
                        }
                        cleanup { sh 'rm -rf tauri/src-tauri/target/release/bundle || true' }
                    }
                }

                // ─── Windows (NSIS .exe) ────────────────────────────────────
                // cmd/bat (the Jenkins `powershell` step's launcher can't find
                // powershell.exe on these agents — PockeoR uses bat too). uv + bun
                // are pre-provisioned in C:\Users\pockeo.
                stage('Windows') {
                    agent { label 'pockeo-windows' }
                    options { retry(count: 2, conditions: [agent(), nonresumable()]) }
                    steps {
                        checkout scm
                        bat '''
                            set "PATH=%USERPROFILE%\\.local\\bin;%USERPROFILE%\\.cargo\\bin;%USERPROFILE%\\.bun\\bin;C:\\Program Files\\Git\\bin;C:\\Program Files\\Git\\usr\\bin;C:\\Program Files\\nodejs;C:\\Strawberry\\perl\\bin;C:\\LLVM\\bin;%PATH%"
                            uv --version || exit /b 1
                            bun --version || exit /b 1
                            rustc --version || exit /b 1

                            if exist backend\\venv rmdir /S /Q backend\\venv
                            uv venv --python 3.12 --seed backend\\venv || exit /b 1
                            backend\\venv\\Scripts\\python.exe -m pip install --upgrade pip || exit /b 1
                            backend\\venv\\Scripts\\pip.exe install -r backend\\requirements.txt || exit /b 1
                            REM build-server.sh auto-installs PyInstaller on unix; do it explicitly here
                            backend\\venv\\Scripts\\pip.exe install pyinstaller || exit /b 1
                            REM CPU sidecar: swap CUDA torch for the CPU build (smaller + correct)
                            backend\\venv\\Scripts\\pip.exe install --index-url https://download.pytorch.org/whl/cpu --force-reinstall torch torchaudio || exit /b 1
                            call bun install || exit /b 1
                            backend\\venv\\Scripts\\python.exe scripts\\ci-disable-updater.py || exit /b 1

                            set "PATH=%CD%\\backend\\venv\\Scripts;%PATH%"
                            python backend\\build_binary.py || exit /b 1
                            for /f "delims=" %%i in ('rustc --print host-tuple') do set "TRIPLE=%%i"
                            if not exist tauri\\src-tauri\\binaries mkdir tauri\\src-tauri\\binaries
                            copy /Y backend\\dist\\voiceit-server.exe "tauri\\src-tauri\\binaries\\voiceit-server-%TRIPLE%.exe" || exit /b 1
                            python backend\\build_binary.py --shim || exit /b 1
                            copy /Y backend\\dist\\voiceit-mcp.exe "tauri\\src-tauri\\binaries\\voiceit-mcp-%TRIPLE%.exe" || exit /b 1

                            REM Drop stale Tauri ACL codegen / fingerprints. After the job + bundle-id
                            REM rename the warm target/ can reference the old workspace path and carry
                            REM mismatched autogenerated permission tomls ("failed to read plugin
                            REM permissions ... app_hide.toml"). Wiping build/ + .fingerprint forces regen.
                            if exist tauri\\src-tauri\\target\\release\\build rmdir /S /Q tauri\\src-tauri\\target\\release\\build
                            if exist tauri\\src-tauri\\target\\release\\.fingerprint rmdir /S /Q tauri\\src-tauri\\target\\release\\.fingerprint

                            cd tauri || exit /b 1
                            call bun run tauri build --bundles nsis || exit /b 1
                        '''
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'tauri/src-tauri/target/release/bundle/nsis/*.exe',
                                allowEmptyArchive: true, fingerprint: true
                        }
                        cleanup {
                            bat 'if exist tauri\\src-tauri\\target\\release\\bundle rmdir /S /Q tauri\\src-tauri\\target\\release\\bundle'
                        }
                    }
                }
            }
        }

        // ─── Release: tag-triggered production bundling ───────────────────
        //
        // Fires only on `v*` git tags (e.g. `v0.8.0`). Does production-grade
        // bundling with real signing + updater artifacts, then publishes a
        // GitHub Release with assets and CHANGELOG-derived notes.
        //
        // ── Prerequisites that must exist before the first tag build ───
        //
        // Jenkins credentials (create at $JENKINS_URL/credentials/store/system):
        //   tauri-signing-key            Secret text  — base64 ed25519 priv key
        //                                              (matches updater pubkey
        //                                              in tauri.conf.json's
        //                                              `plugins.updater.pubkey`)
        //   tauri-signing-key-pw         Secret text  — password for the key
        //   apple-api-key-id             Secret text  — App Store Connect API
        //                                              Key ID (e.g. "ABCD1234")
        //   apple-api-issuer             Secret text  — Issuer UUID for that key
        //   apple-api-key-p8             Secret file  — the .p8 private key
        //   apple-cert-p12               Secret file  — Developer ID cert .p12
        //   apple-cert-password          Secret text  — password for the .p12
        //   apple-signing-identity       Secret text  — "Developer ID
        //                                              Application: <name> (TEAMID)"
        //   github-release-token         Secret text  — PAT with `contents:write`
        //                                              on Tapnetix/VoiceIt for
        //                                              `gh release create`
        //
        // Jenkins agents:
        //   `pockeo-linux`               — existing; .deb + AppImage build
        //   `macos`                      — existing (mbook); macOS arm64 build
        //   `pockeo-windows`             — existing; Windows NSIS + CUDA builds
        //   `macos-intel`                — TO PROVISION: Intel Mac OR Apple
        //                                  Silicon Mac with the
        //                                  x86_64-apple-darwin Rust target +
        //                                  python-pytorch CPU wheel (the
        //                                  `Release: macOS Intel` branch is
        //                                  commented out until this lands).
        //
        // The Release stage is intentionally NOT trying to use the artifacts
        // produced by Build: Build runs `ci-disable-updater.py` and ad-hoc
        // signs on macOS, which makes its bundles unusable for distribution.
        // The Release branches do their own full builds with real keys.
        stage('Release') {
            when { buildingTag() }
            failFast false
            parallel {

                // ─── Linux .deb + AppImage with updater ─────────────────
                stage('Release: Linux') {
                    agent { label 'pockeo-linux' }
                    steps {
                        withCredentials([
                            string(credentialsId: 'tauri-signing-key',    variable: 'TAURI_SIGNING_PRIVATE_KEY'),
                            string(credentialsId: 'tauri-signing-key-pw', variable: 'TAURI_SIGNING_PRIVATE_KEY_PASSWORD'),
                        ]) {
                            sh '''
                                set -eu
                                export PATH="$HOME/.cargo/bin:$HOME/.bun/bin:$HOME/.local/bin:$PATH"
                                command -v bun  >/dev/null 2>&1 || curl -fsSL https://bun.sh/install | bash
                                export PATH="$HOME/.bun/bin:$PATH"
                                command -v just >/dev/null 2>&1 || cargo install just --locked
                                rustc --version; bun --version; just --version; python3 --version

                                if command -v node >/dev/null 2>&1; then node scripts/check-bundle-icons.mjs; else bun scripts/check-bundle-icons.mjs; fi

                                rm -rf backend/venv
                                rm -rf tauri/src-tauri/target/*/build/tauri-* tauri/src-tauri/target/*/.fingerprint/tauri-* 2>/dev/null || true

                                just setup
                                backend/venv/bin/pip install --index-url https://download.pytorch.org/whl/cpu --force-reinstall torch torchaudio
                                backend/venv/bin/pip freeze | grep -iE '^nvidia[-_]' | cut -d'=' -f1 | xargs -r backend/venv/bin/pip uninstall -y || true
                                just build-server
                                # Do NOT run ci-disable-updater.py — we want the
                                # updater enabled in this build so tauri emits
                                # the .deb.sig + AppImage.sig + latest.json
                                # alongside the bundles.
                                ( cd tauri && bun run tauri build --bundles deb,appimage < /dev/null )
                            '''
                        }
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'tauri/src-tauri/target/release/bundle/{deb,appimage}/**',
                                allowEmptyArchive: false, fingerprint: true
                            // Stash for the Publish stage to collect across
                            // agents — archiveArtifacts alone lands them in
                            // the build's archive, but the Publish stage
                            // runs on a fresh Linux agent and can't `cp`
                            // them from this branch's workspace.
                            stash name: 'release-linux',
                                includes: 'tauri/src-tauri/target/release/bundle/deb/*.deb, tauri/src-tauri/target/release/bundle/appimage/*.AppImage*'
                        }
                        cleanup { sh 'rm -rf tauri/src-tauri/target/release/bundle || true' }
                    }
                }

                // ─── macOS arm64 (.dmg) — signed + notarized ───────────
                stage('Release: macOS arm64') {
                    agent { label 'macos' }
                    steps {
                        withCredentials([
                            string(credentialsId: 'tauri-signing-key',     variable: 'TAURI_SIGNING_PRIVATE_KEY'),
                            string(credentialsId: 'tauri-signing-key-pw',  variable: 'TAURI_SIGNING_PRIVATE_KEY_PASSWORD'),
                            string(credentialsId: 'apple-api-key-id',      variable: 'APPLE_API_KEY_ID'),
                            string(credentialsId: 'apple-api-issuer',      variable: 'APPLE_API_ISSUER'),
                            file  (credentialsId: 'apple-api-key-p8',      variable: 'APPLE_API_KEY_P8'),
                            file  (credentialsId: 'apple-cert-p12',        variable: 'APPLE_CERT_P12'),
                            string(credentialsId: 'apple-cert-password',   variable: 'APPLE_CERTIFICATE_PASSWORD'),
                            string(credentialsId: 'apple-signing-identity',variable: 'APPLE_SIGNING_IDENTITY'),
                        ]) {
                            sh '''
                                set -eu
                                export HOME=/Users/jenkins
                                export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/Users/jenkins/.nvm/versions/node/v24.14.1/bin:/Users/jenkins/.nvm/versions/node/v24.14.0/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
                                export BUN_INSTALL="$HOME/.bun"
                                command -v bun >/dev/null 2>&1 || curl -fsSL https://bun.sh/install | bash
                                export PATH="$BUN_INSTALL/bin:$PATH"
                                uv --version; rustc --version; bun --version

                                node scripts/check-bundle-icons.mjs

                                rm -rf backend/venv
                                uv venv --python 3.12 --seed backend/venv
                                backend/venv/bin/python -m pip install --upgrade pip
                                backend/venv/bin/pip install -r backend/requirements.txt
                                backend/venv/bin/pip install -r backend/requirements-mlx.txt
                                backend/venv/bin/pip install --no-deps mlx-lm==0.31.1
                                backend/venv/bin/pip install --no-deps mlx-audio==0.4.1
                                bun install
                                ./scripts/build-server.sh
                                # Keep updater enabled — no ci-disable-updater.py here.
                                rm -rf tauri/src-tauri/target/*/build/tauri-* tauri/src-tauri/target/*/.fingerprint/tauri-* 2>/dev/null || true

                                # Stage the App Store Connect API key for notarytool.
                                mkdir -p "$HOME/.appstoreconnect/private_keys"
                                cp "$APPLE_API_KEY_P8" "$HOME/.appstoreconnect/private_keys/AuthKey_${APPLE_API_KEY_ID}.p8"

                                # Import the Developer ID signing cert into a fresh keychain
                                # so the build can codesign without an interactive prompt.
                                KC="$HOME/Library/Keychains/voiceit-build.keychain-db"
                                rm -f "$KC"
                                security create-keychain -p "" "$KC"
                                security default-keychain -s "$KC"
                                security unlock-keychain -p "" "$KC"
                                security set-keychain-settings -t 3600 -u "$KC"
                                security import "$APPLE_CERT_P12" -k "$KC" -P "$APPLE_CERTIFICATE_PASSWORD" -A -t cert -f pkcs12
                                security set-key-partition-list -S "apple-tool:,apple:,codesign:" -s -k "" "$KC" >/dev/null

                                ( cd tauri && bun run tauri build --bundles dmg --target aarch64-apple-darwin < /dev/null )

                                # Tauri signs the .app with Developer ID and bundles it into the
                                # .dmg, but the .dmg wrapper itself ships unnotarized. Submit it
                                # to notarytool, staple, and verify.
                                DMG_DIR="tauri/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg"
                                shopt -s nullglob
                                dmgs=("${DMG_DIR}"/*.dmg)
                                if [ ${#dmgs[@]} -eq 0 ]; then
                                    echo "::error::No DMGs found in ${DMG_DIR}"
                                    exit 1
                                fi
                                for dmg in "${dmgs[@]}"; do
                                    echo "=== Notarize $(basename "$dmg") ==="
                                    xcrun notarytool submit "$dmg" \
                                        --key   "$HOME/.appstoreconnect/private_keys/AuthKey_${APPLE_API_KEY_ID}.p8" \
                                        --key-id "$APPLE_API_KEY_ID" \
                                        --issuer "$APPLE_API_ISSUER" \
                                        --wait --timeout 20m
                                    xcrun stapler staple "$dmg"
                                    spctl -a -t open --context context:primary-signature -vv "$dmg"
                                done
                            '''
                        }
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'tauri/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/*.dmg, tauri/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/*.tar.gz*',
                                allowEmptyArchive: true, fingerprint: true
                            stash name: 'release-macos-arm64',
                                includes: 'tauri/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg/*.dmg, tauri/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/*.tar.gz*'
                        }
                        cleanup {
                            sh '''
                                rm -rf tauri/src-tauri/target/aarch64-apple-darwin/release/bundle || true
                                security delete-keychain "$HOME/Library/Keychains/voiceit-build.keychain-db" 2>/dev/null || true
                            '''
                        }
                    }
                }

                // ─── macOS Intel (.dmg) — signed + notarized ────────────
                //
                // TODO: enable once a `macos-intel` agent is provisioned.
                // The branch is structurally identical to `Release: macOS arm64`
                // with --target x86_64-apple-darwin and the pytorch backend
                // (no MLX). Leaving commented prevents NodeUnavailable from
                // failing every tag build until the agent exists.
                //
                // stage('Release: macOS Intel') {
                //     agent { label 'macos-intel' }
                //     steps { ... }
                // }

                // ─── Windows NSIS .exe + updater ────────────────────────
                stage('Release: Windows NSIS') {
                    agent { label 'pockeo-windows' }
                    options { retry(count: 2, conditions: [agent(), nonresumable()]) }
                    steps {
                        checkout scm
                        withCredentials([
                            string(credentialsId: 'tauri-signing-key',    variable: 'TAURI_SIGNING_PRIVATE_KEY'),
                            string(credentialsId: 'tauri-signing-key-pw', variable: 'TAURI_SIGNING_PRIVATE_KEY_PASSWORD'),
                        ]) {
                            bat '''
                                set "PATH=%USERPROFILE%\\.local\\bin;%USERPROFILE%\\.cargo\\bin;%USERPROFILE%\\.bun\\bin;C:\\Program Files\\Git\\bin;C:\\Program Files\\Git\\usr\\bin;C:\\Program Files\\nodejs;C:\\Strawberry\\perl\\bin;C:\\LLVM\\bin;%PATH%"
                                uv --version || exit /b 1
                                bun --version || exit /b 1
                                rustc --version || exit /b 1

                                if exist backend\\venv rmdir /S /Q backend\\venv
                                uv venv --python 3.12 --seed backend\\venv || exit /b 1
                                backend\\venv\\Scripts\\python.exe -m pip install --upgrade pip || exit /b 1
                                backend\\venv\\Scripts\\pip.exe install -r backend\\requirements.txt || exit /b 1
                                backend\\venv\\Scripts\\pip.exe install pyinstaller || exit /b 1
                                backend\\venv\\Scripts\\pip.exe install --index-url https://download.pytorch.org/whl/cpu --force-reinstall torch torchaudio || exit /b 1
                                call bun install || exit /b 1
                                REM Do NOT run ci-disable-updater.py — release artifacts include
                                REM the NSIS .exe.sig and latest.json for the updater feed.

                                set "PATH=%CD%\\backend\\venv\\Scripts;%PATH%"
                                python backend\\build_binary.py || exit /b 1
                                for /f "delims=" %%i in ('rustc --print host-tuple') do set "TRIPLE=%%i"
                                if not exist tauri\\src-tauri\\binaries mkdir tauri\\src-tauri\\binaries
                                copy /Y backend\\dist\\voiceit-server.exe "tauri\\src-tauri\\binaries\\voiceit-server-%TRIPLE%.exe" || exit /b 1
                                python backend\\build_binary.py --shim || exit /b 1
                                copy /Y backend\\dist\\voiceit-mcp.exe "tauri\\src-tauri\\binaries\\voiceit-mcp-%TRIPLE%.exe" || exit /b 1

                                if exist tauri\\src-tauri\\target\\release\\build rmdir /S /Q tauri\\src-tauri\\target\\release\\build
                                if exist tauri\\src-tauri\\target\\release\\.fingerprint rmdir /S /Q tauri\\src-tauri\\target\\release\\.fingerprint

                                cd tauri || exit /b 1
                                call bun run tauri build --bundles nsis || exit /b 1
                            '''
                        }
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'tauri/src-tauri/target/release/bundle/nsis/*',
                                allowEmptyArchive: false, fingerprint: true
                            stash name: 'release-windows-nsis',
                                includes: 'tauri/src-tauri/target/release/bundle/nsis/*.exe, tauri/src-tauri/target/release/bundle/nsis/*.exe.sig, tauri/src-tauri/target/release/bundle/nsis/latest.json'
                        }
                        cleanup {
                            bat 'if exist tauri\\src-tauri\\target\\release\\bundle rmdir /S /Q tauri\\src-tauri\\target\\release\\bundle'
                        }
                    }
                }

                // ─── Windows CUDA server tarballs ────────────────────────
                //
                // Builds the separate voiceit-server-cuda PyInstaller onedir,
                // packages it + the cu128 library set into tarballs that the
                // installed app downloads on demand from the GH Release.
                // Reuses the regular `pockeo-windows` agent — no CUDA hardware
                // needed for the build (torch+cu128 wheels carry the libs).
                stage('Release: Windows CUDA') {
                    agent { label 'pockeo-windows' }
                    options { retry(count: 2, conditions: [agent(), nonresumable()]) }
                    steps {
                        checkout scm
                        bat '''
                            set "PATH=%USERPROFILE%\\.local\\bin;%USERPROFILE%\\.cargo\\bin;%USERPROFILE%\\.bun\\bin;C:\\Program Files\\Git\\bin;C:\\Program Files\\Git\\usr\\bin;C:\\Program Files\\nodejs;%PATH%"
                            uv --version || exit /b 1
                            python --version || exit /b 1

                            if exist backend\\venv-cuda rmdir /S /Q backend\\venv-cuda
                            uv venv --python 3.12 --seed backend\\venv-cuda || exit /b 1
                            backend\\venv-cuda\\Scripts\\python.exe -m pip install --upgrade pip || exit /b 1
                            backend\\venv-cuda\\Scripts\\pip.exe install pyinstaller || exit /b 1
                            backend\\venv-cuda\\Scripts\\pip.exe install -r backend\\requirements.txt || exit /b 1
                            REM CUDA wheels:
                            backend\\venv-cuda\\Scripts\\pip.exe install torch      --index-url https://download.pytorch.org/whl/cu128 --force-reinstall --no-deps || exit /b 1
                            backend\\venv-cuda\\Scripts\\pip.exe install torchaudio --index-url https://download.pytorch.org/whl/cu128 --force-reinstall --no-deps || exit /b 1
                            backend\\venv-cuda\\Scripts\\python.exe -c "import torch; print('CUDA build:', torch.version.cuda)" || exit /b 1

                            cd backend || exit /b 1
                            set "TORCH_CUDA_ARCH_LIST=8.0;8.6;8.9;9.0;12.0+PTX"
                            ..\\backend\\venv-cuda\\Scripts\\python.exe build_binary.py --cuda || exit /b 1
                            cd .. || exit /b 1

                            backend\\venv-cuda\\Scripts\\python.exe scripts\\package_cuda.py ^
                                backend\\dist\\voiceit-server-cuda\\ ^
                                --output release-assets\\ ^
                                --cuda-libs-version cu128-v1 ^
                                --torch-compat ">=2.7.0,<2.11.0" || exit /b 1
                        '''
                    }
                    post {
                        success {
                            archiveArtifacts artifacts: 'release-assets/voiceit-server-cuda.tar.gz, release-assets/voiceit-server-cuda.tar.gz.sha256, release-assets/cuda-libs-cu128-v1.tar.gz, release-assets/cuda-libs-cu128-v1.tar.gz.sha256, release-assets/cuda-libs.json',
                                allowEmptyArchive: false, fingerprint: true
                            stash name: 'release-windows-cuda',
                                includes: 'release-assets/*'
                        }
                        cleanup {
                            bat '''
                                if exist backend\\venv-cuda rmdir /S /Q backend\\venv-cuda
                                if exist backend\\dist\\voiceit-server-cuda rmdir /S /Q backend\\dist\\voiceit-server-cuda
                                if exist release-assets rmdir /S /Q release-assets
                            '''
                        }
                    }
                }
            }
        }

        // ─── Publish: collect stashes and create the GitHub Release ───
        //
        // Runs after all Release branches finish. Unstashes the artifacts
        // produced by each parallel branch onto a fresh pockeo-linux
        // workspace, extracts the CHANGELOG section for $TAG_NAME, and
        // creates a DRAFT GitHub Release with `gh release create`. Draft,
        // not published — a human eyeballs assets + notes and hits Publish.
        stage('Publish to GitHub') {
            when { buildingTag() }
            agent { label 'pockeo-linux' }
            steps {
                withCredentials([string(credentialsId: 'github-release-token', variable: 'GH_TOKEN')]) {
                    sh '''
                        set -eu
                        export PATH="$HOME/.local/bin:$PATH"
                        if ! command -v gh >/dev/null 2>&1; then
                            # Bootstrap a local gh into ~/.local/bin (idempotent;
                            # binary persists across builds in the agent home).
                            mkdir -p "$HOME/.local/bin"
                            TMP=$(mktemp -d)
                            curl -fsSL https://github.com/cli/cli/releases/download/v2.65.0/gh_2.65.0_linux_amd64.tar.gz \
                                -o "$TMP/gh.tar.gz"
                            tar -xzf "$TMP/gh.tar.gz" -C "$TMP"
                            cp "$TMP"/gh_*/bin/gh "$HOME/.local/bin/"
                            rm -rf "$TMP"
                        fi
                        gh --version | head -1

                        VERSION="${TAG_NAME#v}"
                        # Extract this version's CHANGELOG section. Pattern
                        # mirrors .github/workflows/release.yml — match from
                        # "## [X.Y.Z]" up to the next "## [" heading and strip
                        # the heading lines themselves.
                        NOTES=$(sed -n "/^## \\[${VERSION}\\]/,/^## \\[/{/^## \\[${VERSION}\\]/d;/^## \\[/d;p;}" CHANGELOG.md)
                        if [ -z "$(printf %s "$NOTES" | tr -d '[:space:]')" ]; then
                            NOTES="See the assets below to download and install this version."
                        fi
                        printf '%s\n' "$NOTES" > /tmp/release-notes.md
                    '''
                    // Pull in the per-platform artifact bundles. unstash drops
                    // them at their original workspace paths.
                    unstash 'release-linux'
                    unstash 'release-macos-arm64'
                    unstash 'release-windows-nsis'
                    unstash 'release-windows-cuda'
                    sh '''
                        set -eu
                        export PATH="$HOME/.local/bin:$PATH"
                        rm -rf release-stage; mkdir -p release-stage
                        # Flatten the per-platform paths into a single dir to
                        # pass to `gh release create`.
                        find tauri/src-tauri/target/release/bundle/deb           -name '*.deb'       -exec cp -v {} release-stage/ \\; 2>/dev/null || true
                        find tauri/src-tauri/target/release/bundle/appimage      -name '*.AppImage*' -exec cp -v {} release-stage/ \\; 2>/dev/null || true
                        find tauri/src-tauri/target/aarch64-apple-darwin/release/bundle/dmg   -name '*.dmg'    -exec cp -v {} release-stage/ \\; 2>/dev/null || true
                        find tauri/src-tauri/target/aarch64-apple-darwin/release/bundle/macos -name '*.tar.gz*' -exec cp -v {} release-stage/ \\; 2>/dev/null || true
                        find tauri/src-tauri/target/release/bundle/nsis          -type f -exec cp -v {} release-stage/ \\; 2>/dev/null || true
                        find release-assets                                      -type f -exec cp -v {} release-stage/ \\; 2>/dev/null || true
                        ls -la release-stage/

                        gh release create "$TAG_NAME" \
                            --repo Tapnetix/VoiceIt \
                            --title "VoiceIt $TAG_NAME" \
                            --notes-file /tmp/release-notes.md \
                            --draft \
                            release-stage/*
                    '''
                }
            }
        }
    }

    post {
        success { echo 'VoiceIt build complete — see archived artifacts above.' }
        failure { echo 'Build failed — see the per-platform stage logs above.' }
    }
}
