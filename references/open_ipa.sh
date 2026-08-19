#!/usr/bin/env bash
# open_ipa.sh — properly open ANY .ipa/.app for IDA-based deobfuscation.
#
# What it does (generic, no per-app hardcoding):
#   1. Unpacks the .ipa (or takes an .app / a bare Mach-O).
#   2. Triages every Mach-O image (app, .appex, frameworks, dylibs):
#      arch · encryption (cryptid) · Swift/ObjC metadata · __TEXT base · size.
#   3. Refuses to waste your time on still-encrypted (cryptid=1) slices — IDA
#      would decompile garbage — and tells you what you actually have.
#   4. Builds a proper .i64 headlessly with full auto-analysis + Swift/ObjC
#      metadata, so ida-pro-mcp attaches to a real DB (not a stray .ipa temp db).
#   5. Writes a targets.md + manifest.json you can drop into a deobf workspace.
#
# Usage:
#   open_ipa.sh <app.ipa | App.app | Mach-O> [options]
# Options:
#   -o, --out DIR        Work dir (default: <input_dir>/<AppName>.ida)
#   -t, --target SPEC    Images to build DBs for:
#                          main (default) · all · appex · frameworks · none
#                          · <substring>   (matches image name, repeatable via commas)
#      --no-analyze      Triage only, don't build any .i64
#      --ida PATH        Path to `idat` (default: autodetect; or $IDA env)
#      --timeout SEC     Per-binary analysis cap (default: 3600)
#   -f, --force          Re-extract and overwrite existing .i64
#   -h, --help
set -u

# ---------- args ----------
IN=""; OUT=""; TARGET="main"; ANALYZE=1; FORCE=0; TIMEOUT=3600; IDA="${IDA:-}"
die(){ printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info(){ printf '%s\n' "$*"; }
while [ $# -gt 0 ]; do
  case "$1" in
    -o|--out) OUT="$2"; shift 2;;
    -t|--target) TARGET="$2"; shift 2;;
    --no-analyze) ANALYZE=0; shift;;
    --ida) IDA="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    -f|--force) FORCE=1; shift;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*) die "unknown option: $1";;
    *) [ -z "$IN" ] && IN="$1" || die "unexpected arg: $1"; shift;;
  esac
done
[ -n "$IN" ] || die "no input. Try: $(basename "$0") App.ipa"
[ -e "$IN" ] || die "not found: $IN"

# ---------- locate idat ----------
if [ "$ANALYZE" = 1 ]; then
  if [ -z "$IDA" ]; then
    for c in "/Applications/IDA Professional 9.3.app/Contents/MacOS/idat" \
             /Applications/IDA*Professional*.app/Contents/MacOS/idat \
             /Applications/IDA*.app/Contents/MacOS/idat \
             "$HOME"/Applications/IDA*.app/Contents/MacOS/idat; do
      [ -x "$c" ] && { IDA="$c"; break; }
    done
  fi
  [ -n "$IDA" ] && [ -x "$IDA" ] || die "idat not found — pass --ida /path/to/idat or --no-analyze"
fi

# ---------- helpers ----------
is_macho(){ # $1=file -> 0 if Mach-O (thin or fat)
  local m; m=$(head -c4 "$1" 2>/dev/null | xxd -p 2>/dev/null) || return 1
  case "$m" in feedface|cefaedfe|feedfacf|cffaedfe|cafebabe|bebafeca|cafebabf|bfbafeca) return 0;; *) return 1;; esac
}
macho_arch(){ lipo -archs "$1" 2>/dev/null || echo "?"; }
macho_fat(){ lipo -info "$1" 2>/dev/null | grep -q "^Architectures in the fat" && echo 1 || echo 0; }
macho_cryptid(){ # prints cryptid number, or "-" if no LC_ENCRYPTION_INFO
  otool -l "$1" 2>/dev/null | awk '/LC_ENCRYPTION_INFO/{f=1} f&&/cryptid/{print $2; exit}' | grep -q . \
    && otool -l "$1" 2>/dev/null | awk '/LC_ENCRYPTION_INFO/{f=1} f&&/cryptid/{print $2; exit}' || echo "-"
}
macho_textbase(){ otool -l "$1" 2>/dev/null | awk '/segname __TEXT/{f=1} f&&/vmaddr/{print $2; exit}'; }
macho_meta(){ # Swift/ObjC flags
  local s; s=$(otool -l "$1" 2>/dev/null | grep -oE "__swift5_|__objc_" | sort -u | tr -d '\n')
  local o=""; case "$s" in *__swift5_*) o="Swift";; esac
  case "$s" in *__objc_*) o="${o:+$o+}ObjC";; esac; echo "${o:-—}"
}

# ---------- resolve input into a work dir + list of Mach-O ----------
BASENAME="$(basename "$IN")"
INDIR="$(cd "$(dirname "$IN")" && pwd)"
APP_DIR=""; MAIN_BIN=""; EXTRACTED=""
declare -a IMG_PATH IMG_ROLE IMG_NAME

add_bundle_images(){ # $1 = *.app dir
  local app="$1" exe
  exe="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app/Info.plist" 2>/dev/null)"
  [ -n "$exe" ] && [ -f "$app/$exe" ] && { IMG_PATH+=("$app/$exe"); IMG_ROLE+=("app"); IMG_NAME+=("$exe"); MAIN_BIN="$app/$exe"; }
  local d b
  for d in "$app"/PlugIns/*.appex; do
    [ -d "$d" ] || continue
    b="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$d/Info.plist" 2>/dev/null)"
    [ -n "$b" ] && [ -f "$d/$b" ] && { IMG_PATH+=("$d/$b"); IMG_ROLE+=("appex"); IMG_NAME+=("$b"); }
  done
  for d in "$app"/Frameworks/*.framework; do
    [ -d "$d" ] || continue
    b="$(basename "$d" .framework)"
    [ -f "$d/$b" ] && is_macho "$d/$b" && { IMG_PATH+=("$d/$b"); IMG_ROLE+=("framework"); IMG_NAME+=("$b"); }
  done
  for f in "$app"/Frameworks/*.dylib; do
    [ -f "$f" ] && is_macho "$f" && { IMG_PATH+=("$f"); IMG_ROLE+=("dylib"); IMG_NAME+=("$(basename "$f")"); }
  done
}

case "$IN" in
  *.ipa|*.zip)
    OUT="${OUT:-$INDIR/${BASENAME%.*}.ida}"
    EXTRACTED="$OUT/extracted"
    if [ "$FORCE" = 1 ] || [ ! -d "$EXTRACTED/Payload" ]; then
      rm -rf "$EXTRACTED"; mkdir -p "$EXTRACTED"
      info "▸ unzip $BASENAME → $EXTRACTED"
      unzip -q -o "$IN" -d "$EXTRACTED" || die "unzip failed"
    else
      info "▸ reuse existing extraction ($EXTRACTED) — pass -f to redo"
    fi
    APP_DIR="$(ls -d "$EXTRACTED"/Payload/*.app 2>/dev/null | head -1)"
    [ -n "$APP_DIR" ] || die "no Payload/*.app inside the archive"
    add_bundle_images "$APP_DIR" ;;
  *.app)
    APP_DIR="$(cd "$IN" && pwd)"
    OUT="${OUT:-$INDIR/$(basename "$APP_DIR" .app).ida}"
    mkdir -p "$OUT"
    add_bundle_images "$APP_DIR" ;;
  *)
    is_macho "$IN" || die "not an .ipa, .app, or Mach-O: $IN"
    OUT="${OUT:-$INDIR/${BASENAME}.ida}"; mkdir -p "$OUT"
    IMG_PATH+=("$(cd "$INDIR" && pwd)/$BASENAME"); IMG_ROLE+=("macho"); IMG_NAME+=("$BASENAME")
    MAIN_BIN="${IMG_PATH[0]}" ;;
esac
mkdir -p "$OUT/dbs"
[ "${#IMG_PATH[@]}" -gt 0 ] || die "no Mach-O images found"

# ---------- triage table ----------
info ""
info "  ROLE        NAME                                  ARCH        CRYPT  META        SIZE"
info "  ────────────────────────────────────────────────────────────────────────────────────"
ENC_WARN=0
declare -a T_CID T_BASE
for i in "${!IMG_PATH[@]}"; do
  p="${IMG_PATH[$i]}"
  arch="$(macho_arch "$p")"; cid="$(macho_cryptid "$p")"; meta="$(macho_meta "$p")"
  base="$(macho_textbase "$p")"; sz="$(stat -f%z "$p" 2>/dev/null)"
  T_CID[$i]="$cid"; T_BASE[$i]="$base"
  cflag="$cid"; [ "$cid" = "0" ] && cflag="ok"; [ "$cid" = "-" ] && cflag="n/a"
  mark=""; [ "$cid" = "1" ] && { mark=" ⚠ ENCRYPTED"; ENC_WARN=1; }
  printf "  %-11s %-37s %-11s %-6s %-11s %8s%s\n" \
    "${IMG_ROLE[$i]}" "${IMG_NAME[$i]:0:37}" "$arch" "$cflag" "$meta" \
    "$(printf '%d' "${sz:-0}" | awk '{printf "%.1fM", $1/1048576}')" "$mark"
done
info ""
info "  base rule: vaddr = <__TEXT base> + file_offset   (arm64 exe base is usually 0x100000000)"
[ "$ENC_WARN" = 1 ] && {
  info ""
  info "  ⚠  One or more slices are still FairPlay-encrypted (cryptid=1)."
  info "     IDA will produce garbage on those. You need an already-decrypted copy of the"
  info "     binary (cryptid must be 0) before analysis."
}

# ---------- select targets ----------
declare -a SEL
want(){ # $1 index -> 0 if selected by $TARGET
  local role="${IMG_ROLE[$1]}" name="${IMG_NAME[$1]}" p="${IMG_PATH[$1]}"
  case "$TARGET" in
    none) return 1;;
    all)  return 0;;
    main) [ "$p" = "$MAIN_BIN" ] && return 0 || return 1;;
    appex) [ "$role" = "appex" ] && return 0 || return 1;;
    frameworks|framework) { [ "$role" = "framework" ] || [ "$role" = "dylib" ]; } && return 0 || return 1;;
    *) # comma-list of substrings on the name
       local IFS=,; for t in $TARGET; do case "$name" in *"$t"*) return 0;; esac; done; return 1;;
  esac
}
for i in "${!IMG_PATH[@]}"; do want "$i" && SEL+=("$i"); done

if [ "$ANALYZE" = 0 ]; then
  info ""; info "▸ triage only (--no-analyze). Work dir: $OUT"; exit 0
fi
[ "${#SEL[@]}" -gt 0 ] || { info ""; info "▸ nothing matched --target '$TARGET' — nothing to build. Work dir: $OUT"; exit 0; }

# ---------- analyze.py (auto-analysis + wait + save + quit) ----------
APY="$OUT/.analyze.py"
cat > "$APY" <<'PY'
import ida_auto, ida_pro, idaapi
ida_auto.auto_wait()
try: print("ANALYZE_DONE funcs=%d" % idaapi.get_func_qty())
except Exception as e: print("ANALYZE_DONE funcs=?", e)
ida_pro.qexit(0)
PY

run_timeout(){ # $1=sec ; rest=cmd  (portable timeout with progress)
  local secs="$1"; shift
  "$@" >"$OUT/.build.out" 2>&1 & local pid=$! t=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5; t=$((t+5))
    [ $((t%30)) -eq 0 ] && printf '    …%ss\n' "$t"
    [ "$t" -ge "$secs" ] && { kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return 124; }
  done
  wait "$pid" 2>/dev/null; return $?
}

# ---------- build DBs ----------
info ""; info "▸ building databases with: $IDA"
declare -a DB_FOR
for i in "${SEL[@]}"; do
  src="${IMG_PATH[$i]}"; name="${IMG_NAME[$i]}"; role="${IMG_ROLE[$i]}"
  [ "${T_CID[$i]}" = "1" ] && { info "  skip $name — encrypted (cryptid=1)"; continue; }
  # thin fat binaries to arm64 for a clean DB
  work="$src"
  if [ "$(macho_fat "$src")" = "1" ]; then
    work="$OUT/dbs/${name}.arm64"
    lipo "$src" -thin arm64 -output "$work" 2>/dev/null || lipo "$src" -thin arm64e -output "$work" 2>/dev/null || work="$src"
  fi
  db="$OUT/dbs/${name}.i64"
  if [ -f "$db" ] && [ "$FORCE" != 1 ]; then info "  have $name.i64 (‑f to rebuild)"; DB_FOR[$i]="$db"; continue; fi
  rm -f "$db"
  printf '  %-32s ' "$name ($role)…"
  start=$SECONDS
  if run_timeout "$TIMEOUT" "$IDA" -A -c -o"$db" -S"$APY" -L"$OUT/dbs/${name}.log" "$work"; then
    fn=$(grep -o "ANALYZE_DONE funcs=[0-9]*" "$OUT/dbs/${name}.log" 2>/dev/null | head -1 | grep -o "[0-9]*$")
    printf 'ok  %ss  %s funcs  →  dbs/%s.i64\n' "$((SECONDS-start))" "${fn:-?}" "$name"
    DB_FOR[$i]="$db"
  else
    printf 'FAILED (see dbs/%s.log)\n' "$name"
  fi
done

# ---------- manifest ----------
TM="$OUT/targets.md"
{
  echo "# Targets — $(basename "$APP_DIR" .app 2>/dev/null || echo "$BASENAME")"
  echo
  echo "Generated by open_ipa.sh. Base rule: \`vaddr = <__TEXT base> + file_off\`."
  echo
  echo "| # | Role | Binary | Arch | Crypt | Meta | __TEXT base | IDA db |"
  echo "|---|------|--------|------|-------|------|-------------|--------|"
  n=0
  for i in "${!IMG_PATH[@]}"; do
    n=$((n+1)); cid="${T_CID[$i]}"; [ "$cid" = "0" ] && cid="ok"; [ "$cid" = "-" ] && cid="n/a"
    dbc="—"; [ -n "${DB_FOR[$i]:-}" ] && dbc="\`dbs/${IMG_NAME[$i]}.i64\`"
    echo "| $n | ${IMG_ROLE[$i]} | \`${IMG_NAME[$i]}\` | $(macho_arch "${IMG_PATH[$i]}") | $cid | $(macho_meta "${IMG_PATH[$i]}") | ${T_BASE[$i]:-?} | $dbc |"
  done
  echo
  echo "## One live database at a time"
  echo "ida-pro-mcp / hopper-mcp each drive ONE open document. Open a single .i64 in IDA,"
  echo "run the deobf loop against it, then switch documents to move to the next target."
} > "$TM"

# JSON manifest
JM="$OUT/manifest.json"
{
  echo "{"
  echo "  \"input\": \"$IN\","
  echo "  \"app\": \"$(basename "${APP_DIR:-$BASENAME}")\","
  echo "  \"out\": \"$OUT\","
  echo "  \"images\": ["
  last=$(( ${#IMG_PATH[@]} - 1 ))
  for i in "${!IMG_PATH[@]}"; do
    comma=","; [ "$i" -eq "$last" ] && comma=""
    printf '    {"name":"%s","role":"%s","arch":"%s","cryptid":"%s","textbase":"%s","db":"%s","path":"%s"}%s\n' \
      "${IMG_NAME[$i]}" "${IMG_ROLE[$i]}" "$(macho_arch "${IMG_PATH[$i]}")" "${T_CID[$i]}" \
      "${T_BASE[$i]:-}" "${DB_FOR[$i]:-}" "${IMG_PATH[$i]}" "$comma"
  done
  echo "  ]"
  echo "}"
} > "$JM"

info ""
info "▸ done. Work dir: $OUT"
info "  manifest : $TM"
info "  next     : open a .i64 in IDA GUI so ida-pro-mcp attaches, e.g."
for i in "${SEL[@]}"; do [ -n "${DB_FOR[$i]:-}" ] && { info "               open -a \"IDA Professional 9.3\" \"${DB_FOR[$i]}\""; break; }; done
