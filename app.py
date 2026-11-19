#!/usr/bin/env python3
"""
Tela de configuração + orquestrador do go2rtc, só com biblioteca padrão do Python.
Roda com: py app.py
Abre http://localhost:8766/ para configurar as câmeras (Tapo e DVR JFL/Hikvision).
Depois de salvar, o go2rtc sobe sozinho em http://localhost:1984/ com a grade de vídeo.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import json
import platform
import re
import socket
import subprocess
import threading
import time
import atexit
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

DIR = Path(__file__).resolve().parent
YAML_PATH = DIR / "go2rtc.yaml"
CAMERAS_PATH = DIR / "cameras.json"
CAMERAS_JS_PATH = DIR / "web" / "cameras.generated.js"
BIN_DIR = DIR / "bin"
GO2RTC_EXE = BIN_DIR / ("go2rtc.exe" if platform.system() == "Windows" else "go2rtc")

PORTA_CONFIG = 8766

_go2rtc_proc = None
_lock = threading.Lock()


# --------------------------------------------------------------------------
# Dados
# --------------------------------------------------------------------------

def carregar_cameras():
    if not CAMERAS_PATH.exists():
        return []
    try:
        return json.loads(CAMERAS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def salvar_cameras(cams):
    CAMERAS_PATH.write_text(json.dumps(cams, ensure_ascii=False, indent=2), encoding="utf-8")


def senhas_atuais_do_yaml():
    """Lê o go2rtc.yaml existente e devolve {id: (usuario, senha_decodificada)} pra reaproveitar
    senhas não reenviadas na edição."""
    resultado = {}
    if not YAML_PATH.exists():
        return resultado
    texto = YAML_PATH.read_text(encoding="utf-8")
    # bloco "  tapo1:\n    - rtsp://user:pass@ip:554/..."
    for m in re.finditer(r'^  (\w+):\n    - rtsp://([^:]+):([^@]+)@', texto, re.MULTILINE):
        cam_id, user, senha_cod = m.group(1), m.group(2), m.group(3)
        if cam_id.endswith("_leve"):
            continue
        resultado[cam_id] = (user, unquote(senha_cod))
    return resultado


def proximo_id(cams, tipo, canal=None):
    if tipo == "dvr":
        return f"dvr{canal}"
    usados = {c["id"] for c in cams if c["tipo"] == "tapo"}
    n = 1
    while f"tapo{n}" in usados:
        n += 1
    return f"tapo{n}"


def gerar_yaml(cams, senhas_por_id):
    linhas = []
    for c in cams:
        cid = c["id"]
        user, senha = senhas_por_id.get(cid, ("", ""))
        senha_cod = quote(senha, safe="")
        ip = c["ip"]
        if c["tipo"] == "tapo":
            linhas.append(f"  {cid}:")
            linhas.append(f"    - rtsp://{user}:{senha_cod}@{ip}:554/stream1")
            linhas.append(f"  {cid}_leve:")
            linhas.append(f"    - rtsp://{user}:{senha_cod}@{ip}:554/stream2")
        else:  # dvr
            canal = c.get("canal", 1)
            linhas.append(f"  {cid}:")
            linhas.append(f"    - rtsp://{user}:{senha_cod}@{ip}:554/Streaming/Channels/{canal}01")
            linhas.append(f"  {cid}_leve:")
            linhas.append(f"    - rtsp://{user}:{senha_cod}@{ip}:554/Streaming/Channels/{canal}02")

    corpo = "\n".join(linhas) if linhas else "  {}"
    yaml = f"""api:
  listen: ":1984"
  static_dir: web

rtsp:
  listen: ":8554"

streams:
{corpo}

webrtc:
  listen: ":8555"
"""
    YAML_PATH.write_text(yaml, encoding="utf-8")


def gerar_viewer(cams):
    """Escreve web/cameras.generated.js — nunca mexe no index.html (que fica genérico
    e vai pro Git). Este arquivo .js é ignorado pelo Git (guarda nomes/IDs reais)."""
    itens = ",\n".join(
        f'  {{ nome: {json.dumps(c["nome"], ensure_ascii=False)}, alta: "{c["id"]}", leve: "{c["id"]}_leve" }}'
        for c in cams
    )
    conteudo = (
        "// Gerado automaticamente pelo app.py — não edite à mão.\n"
        f"export const CAMERAS = [\n{itens}{',' if itens else ''}\n];\n"
    )
    CAMERAS_JS_PATH.write_text(conteudo, encoding="utf-8")


def aplicar(cams):
    """Grava yaml + viewer e (re)inicia o go2rtc."""
    senhas = senhas_atuais_do_yaml()
    for c in cams:
        if c.get("_senha_nova"):
            senhas[c["id"]] = (c["usuario"], c.pop("_senha_nova"))
        c.pop("_senha_nova", None)
    gerar_yaml(cams, senhas)
    gerar_viewer(cams)
    reiniciar_go2rtc()


def _nome_pacote_go2rtc():
    sistema = platform.system()
    maquina = platform.machine().lower()
    if sistema == "Windows":
        return "go2rtc_win64.zip" if "64" in maquina else "go2rtc_win32.zip"
    if sistema == "Linux":
        return "go2rtc_linux_arm64" if "aarch64" in maquina or "arm64" in maquina else "go2rtc_linux_amd64"
    if sistema == "Darwin":
        return "go2rtc_mac_arm64" if "arm" in maquina else "go2rtc_mac_amd64"
    return None


def garantir_go2rtc():
    """Baixa o go2rtc.exe (binário único, MIT) do GitHub na primeira vez que falta,
    pra não exigir nenhum passo manual de instalação."""
    if GO2RTC_EXE.exists():
        return True
    pacote = _nome_pacote_go2rtc()
    if not pacote:
        print("Não reconheci seu sistema automaticamente.")
        print("Baixe manualmente em https://github.com/AlexxIT/go2rtc/releases e coloque em bin/")
        return False
    print(f"go2rtc não encontrado — baixando ({pacote})...")
    BIN_DIR.mkdir(exist_ok=True)
    try:
        with urllib.request.urlopen(
            "https://api.github.com/repos/AlexxIT/go2rtc/releases/latest", timeout=15
        ) as r:
            info = json.loads(r.read())
        asset = next((a for a in info["assets"] if a["name"] == pacote), None)
        if not asset:
            raise RuntimeError(f"pacote {pacote} não está na última versão publicada")
        baixado = BIN_DIR / pacote
        urllib.request.urlretrieve(asset["browser_download_url"], baixado)
        if pacote.endswith(".zip"):
            with zipfile.ZipFile(baixado) as z:
                z.extractall(BIN_DIR)
            baixado.unlink()
        else:
            baixado.rename(GO2RTC_EXE)
            GO2RTC_EXE.chmod(GO2RTC_EXE.stat().st_mode | 0o111)  # sem efeito no Windows
        print(f"go2rtc baixado em {GO2RTC_EXE}")
        return True
    except Exception as e:
        print(f"Falha ao baixar o go2rtc automaticamente: {e}")
        print("Baixe manualmente em https://github.com/AlexxIT/go2rtc/releases e coloque em bin/")
        return False


def reiniciar_go2rtc():
    global _go2rtc_proc
    with _lock:
        if _go2rtc_proc and _go2rtc_proc.poll() is None:
            _go2rtc_proc.terminate()
            try:
                _go2rtc_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _go2rtc_proc.kill()
        if not GO2RTC_EXE.exists() and not garantir_go2rtc():
            print("AVISO: go2rtc indisponível — não deu pra iniciar a grade de vídeo.")
            return
        if not CAMERAS_PATH.exists() or not carregar_cameras():
            return  # nada configurado ainda, não sobe à toa
        _go2rtc_proc = subprocess.Popen(
            [str(GO2RTC_EXE), "-config", str(YAML_PATH)],
            cwd=str(DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"go2rtc iniciado (PID {_go2rtc_proc.pid})")


def parar_go2rtc():
    global _go2rtc_proc
    if _go2rtc_proc and _go2rtc_proc.poll() is None:
        _go2rtc_proc.terminate()


atexit.register(parar_go2rtc)


def ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def testar_porta(ip, porta=554, timeout=1.5):
    try:
        with socket.create_connection((ip, porta), timeout=timeout):
            return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Página de configuração (HTML embutido, sem dependência externa)
# --------------------------------------------------------------------------

PAGINA = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Configurar câmeras</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0b0d10; --card: #141821; --borda: #262c38; --borda-clara: #323a49;
    --texto: #e8eaed; --texto-fraco: #8d97a8; --azul: #2d7dfb; --azul-escuro: #1f5fd1;
    --verde: #3fb950; --vermelho: #f85149; --vermelho-escuro: #932b2b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--texto);
    font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
    padding: 24px 20px 60px; max-width: 760px; margin-inline: auto; line-height: 1.4;
  }
  .topo { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
  .topo .icone { font-size: 30px; line-height: 1; }
  .topo h1 { font-size: 20px; margin: 0; }
  .topo p { margin: 2px 0 0; font-size: 13px; color: var(--texto-fraco); }

  .status {
    background: var(--card); border: 1px solid var(--borda); border-radius: 12px;
    padding: 14px 18px; margin-bottom: 22px; font-size: 14px; display: flex;
    flex-wrap: wrap; align-items: center; gap: 8px 16px;
  }
  .status a { color: var(--azul); text-decoration: none; }
  .status a:hover { text-decoration: underline; }
  .bolinha { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .bolinha.ok { background: var(--verde); box-shadow: 0 0 6px var(--verde); }
  .bolinha.erro { background: var(--vermelho); }
  .status .separador { color: var(--borda-clara); }
  .status .linha-secundaria { flex-basis: 100%; font-size: 12.5px; color: var(--texto-fraco); }

  h2.titulo-secao { font-size: 13px; text-transform: uppercase; letter-spacing: .05em; color: var(--texto-fraco); margin: 0 0 10px 2px; font-weight: 600; }

  .cam {
    background: var(--card); border: 1px solid var(--borda); border-radius: 12px;
    padding: 12px 14px; margin-bottom: 8px; display: flex; align-items: center; gap: 12px;
    transition: border-color .15s;
  }
  .cam:hover { border-color: var(--borda-clara); }
  .cam .avatar {
    width: 38px; height: 38px; border-radius: 9px; background: #1c2230; display: flex;
    align-items: center; justify-content: center; font-size: 18px; flex-shrink: 0;
  }
  .cam .info { flex: 1; min-width: 0; }
  .cam .info b { display: block; font-size: 15px; font-weight: 600; }
  .cam .info span { color: var(--texto-fraco); font-size: 12.5px; }
  .cam .acoes { display: flex; gap: 6px; flex-shrink: 0; }
  .cam .acoes button { margin: 0; padding: 7px 12px; font-size: 13px; }
  .alca { cursor: grab; color: #4a5263; font-size: 18px; padding: 0 4px; user-select: none; touch-action: none; flex-shrink: 0; }
  .alca:active { cursor: grabbing; }
  .cam.arrastando { opacity: .35; }
  .cam.sobre-linha-cima { border-top: 2px solid var(--azul); }
  .cam.sobre-linha-baixo { border-bottom: 2px solid var(--azul); }
  .vazio { color: var(--texto-fraco); font-size: 14px; padding: 18px 4px; }

  form {
    background: var(--card); border: 1px solid var(--borda); border-radius: 12px;
    padding: 20px; margin-top: 26px;
  }
  form h3 { margin: 0 0 4px; font-size: 16px; }
  .campos { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0 14px; }
  .campo-largo { grid-column: 1 / -1; }
  label { display: block; margin-top: 14px; font-size: 12.5px; color: var(--texto-fraco); font-weight: 500; }
  label .dica { color: #5b6472; font-weight: 400; }
  input, select {
    width: 100%; padding: 10px 11px; margin-top: 5px; border-radius: 8px;
    border: 1px solid #30363d; background: var(--bg); color: var(--texto); font-size: 14px;
    transition: border-color .15s;
  }
  input:focus, select:focus { outline: none; border-color: var(--azul); }
  select { appearance: none; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%238d97a8' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; padding-right: 30px; }
  button {
    background: var(--azul); color: white; border: none; border-radius: 8px;
    padding: 10px 18px; font-size: 14px; font-weight: 600; cursor: pointer; margin-top: 18px;
    transition: background .15s;
  }
  button:hover { background: var(--azul-escuro); }
  button.secundario { background: #262c38; }
  button.secundario:hover { background: #313847; }
  button.perigo { background: transparent; color: var(--vermelho); border: 1px solid #4a2626; }
  button.perigo:hover { background: var(--vermelho-escuro); color: white; }
  .linha-botoes { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  #campoCanal { display: none; }
  .ok { color: var(--verde); } .erro { color: var(--vermelho); }
  #msg { font-size: 13px; }
</style>
</head>
<body>
<div class="topo">
  <div class="icone">📷</div>
  <div>
    <h1>Configurar câmeras</h1>
    <p>Tapo e DVR, só na rede local — sem nuvem, sem app de terceiro</p>
  </div>
</div>
<div class="status" id="status">Carregando...</div>

<h2 class="titulo-secao">Câmeras cadastradas</h2>
<div id="lista"></div>

<form id="form">
  <h3 id="tituloForm">Adicionar câmera</h3>
  <input type="hidden" id="id" value="">
  <div class="campos">
    <div class="campo-largo">
      <label>Tipo</label>
      <select id="tipo">
        <option value="tapo">📷 Câmera Tapo</option>
        <option value="dvr">🖥️ Canal do DVR (JFL / Hikvision)</option>
      </select>
    </div>
    <div class="campo-largo" id="campoCanal">
      <label>Número do canal no DVR (1 a 4)</label>
      <input type="number" id="canal" min="1" max="16" value="1">
    </div>
    <div>
      <label>Nome para exibir</label>
      <input type="text" id="nome" placeholder="Ex: Garagem">
    </div>
    <div>
      <label>IP na rede local</label>
      <input type="text" id="ip" placeholder="Ex: 192.168.10.110">
    </div>
    <div>
      <label>Usuário</label>
      <input type="text" id="usuario" placeholder="admin" autocomplete="off">
    </div>
    <div>
      <label>Senha <span class="dica" id="dicaSenha" style="display:none">(em branco mantém a atual)</span></label>
      <input type="password" id="senha" autocomplete="new-password">
    </div>
  </div>
  <div class="linha-botoes">
    <button type="submit">Salvar e aplicar</button>
    <button type="button" class="secundario" id="cancelar" style="display:none">Cancelar edição</button>
    <span id="msg"></span>
  </div>
</form>

<script>
const tipoSel = document.getElementById("tipo");
const campoCanal = document.getElementById("campoCanal");
tipoSel.addEventListener("change", () => campoCanal.style.display = tipoSel.value === "dvr" ? "block" : "none");

let emArrasto = false;

function limparLinhasDrag() {
  document.querySelectorAll(".cam").forEach(el => el.classList.remove("sobre-linha-cima", "sobre-linha-baixo"));
}

async function salvarOrdem() {
  const ids = [...document.getElementById("lista").querySelectorAll(".cam")].map(el => el.dataset.id);
  await fetch("/api/ordem", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(ids) });
}

async function carregarTudo() {
  if (emArrasto) return; // não redesenha a lista enquanto está arrastando
  const [cams, status] = await Promise.all([
    fetch("/api/cameras").then(r => r.json()),
    fetch("/api/status").then(r => r.json()),
  ]);
  const lista = document.getElementById("lista");
  lista.innerHTML = cams.length ? "" : '<p class="vazio">Nenhuma câmera cadastrada ainda — adicione uma no formulário abaixo.</p>';
  cams.forEach(c => {
    const div = document.createElement("div");
    div.className = "cam";
    div.draggable = true;
    div.dataset.id = c.id;
    div.innerHTML = `<span class="alca" title="Arraste para reordenar">⠿</span>
      <div class="avatar">${c.tipo === "dvr" ? "🖥️" : "📷"}</div>
      <div class="info"><b></b><span></span></div>
      <div class="acoes"><button class="secundario" data-editar>Editar</button><button class="perigo" data-remover>Remover</button></div>`;
    div.querySelector("b").textContent = c.nome;
    div.querySelector("span").textContent = (c.tipo === "dvr" ? `DVR canal ${c.canal} · ` : "Tapo · ") + c.ip;
    div.querySelector("[data-editar]").onclick = () => editar(c);
    div.querySelector("[data-remover]").onclick = () => remover(c.id, c.nome);

    div.addEventListener("dragstart", (e) => {
      emArrasto = true;
      div.classList.add("arrastando");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", c.id);
    });
    div.addEventListener("dragover", (e) => {
      e.preventDefault();
      limparLinhasDrag();
      const meio = div.getBoundingClientRect().top + div.getBoundingClientRect().height / 2;
      div.classList.add(e.clientY < meio ? "sobre-linha-cima" : "sobre-linha-baixo");
    });
    div.addEventListener("drop", (e) => {
      e.preventDefault();
      const idArrastado = e.dataTransfer.getData("text/plain");
      const elArrastado = lista.querySelector(`[data-id="${idArrastado}"]`);
      if (!elArrastado || elArrastado === div) { limparLinhasDrag(); return; }
      const meio = div.getBoundingClientRect().top + div.getBoundingClientRect().height / 2;
      lista.insertBefore(elArrastado, e.clientY < meio ? div : div.nextSibling);
      limparLinhasDrag();
    });
    div.addEventListener("dragend", () => {
      div.classList.remove("arrastando");
      limparLinhasDrag();
      salvarOrdem().finally(() => { emArrasto = false; });
    });

    lista.appendChild(div);
  });

  const s = document.getElementById("status");
  const corGo2rtc = status.go2rtc_rodando ? "ok" : "erro";
  s.innerHTML = `
    <span><span class="bolinha ${corGo2rtc}"></span>go2rtc ${status.go2rtc_rodando ? "rodando" : "parado"}</span>
    ${status.go2rtc_rodando
      ? `<span class="separador">·</span><span>🖥️ Grade de vídeo: <a href="http://${status.ip_local}:${status.porta_video}/" target="_blank">http://${status.ip_local}:${status.porta_video}/</a></span>`
      : `<span class="separador">·</span><span>adicione uma câmera abaixo para iniciar</span>`}
    <span class="linha-secundaria">Esta tela (acessível também pela TV/celular na mesma rede): <a href="http://${status.ip_local}:${status.porta_config}/">http://${status.ip_local}:${status.porta_config}/</a></span>`;
}

function editar(c) {
  document.getElementById("tituloForm").textContent = "Editando: " + c.nome;
  document.getElementById("id").value = c.id;
  document.getElementById("tipo").value = c.tipo;
  campoCanal.style.display = c.tipo === "dvr" ? "block" : "none";
  document.getElementById("canal").value = c.canal || 1;
  document.getElementById("nome").value = c.nome;
  document.getElementById("ip").value = c.ip;
  document.getElementById("usuario").value = c.usuario;
  document.getElementById("senha").value = "";
  document.getElementById("dicaSenha").style.display = "inline";
  document.getElementById("cancelar").style.display = "inline-block";
  window.scrollTo({ top: document.getElementById("form").offsetTop - 20, behavior: "smooth" });
}

document.getElementById("cancelar").onclick = () => {
  document.getElementById("form").reset();
  document.getElementById("id").value = "";
  document.getElementById("tituloForm").textContent = "Adicionar câmera";
  document.getElementById("dicaSenha").style.display = "none";
  document.getElementById("cancelar").style.display = "none";
  campoCanal.style.display = "none";
};

async function remover(id, nome) {
  if (!confirm(`Remover "${nome}"?`)) return;
  await fetch("/api/cameras/" + id, { method: "DELETE" });
  carregarTudo();
}

document.getElementById("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const corpo = {
    id: document.getElementById("id").value || null,
    tipo: document.getElementById("tipo").value,
    canal: parseInt(document.getElementById("canal").value || "1", 10),
    nome: document.getElementById("nome").value.trim(),
    ip: document.getElementById("ip").value.trim(),
    usuario: document.getElementById("usuario").value.trim(),
    senha: document.getElementById("senha").value,
  };
  const msg = document.getElementById("msg");
  if (!corpo.nome || !corpo.ip || !corpo.usuario) {
    msg.innerHTML = '<span class="erro">Preencha nome, IP e usuário.</span>';
    return;
  }
  if (!corpo.id && !corpo.senha) {
    msg.innerHTML = '<span class="erro">Informe a senha para uma câmera nova.</span>';
    return;
  }
  msg.textContent = "Salvando e reiniciando o go2rtc...";
  const r = await fetch("/api/cameras", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo) });
  if (r.ok) {
    msg.innerHTML = '<span class="ok">Salvo!</span>';
    document.getElementById("cancelar").onclick();
    carregarTudo();
  } else {
    msg.innerHTML = '<span class="erro">Erro ao salvar: ' + await r.text() + '</span>';
  }
});

carregarTudo();
setInterval(carregarTudo, 5000);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Servidor HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencioso

    def _json(self, obj, codigo=200):
        corpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _texto(self, texto, codigo=200, tipo="text/html; charset=utf-8"):
        corpo = texto.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._texto(PAGINA)
        elif self.path == "/api/cameras":
            cams = carregar_cameras()
            pub = [{k: v for k, v in c.items() if k != "_senha_nova"} for c in cams]
            self._json(pub)
        elif self.path == "/api/status":
            global _go2rtc_proc
            rodando = bool(_go2rtc_proc and _go2rtc_proc.poll() is None)
            self._json({
                "go2rtc_rodando": rodando,
                "ip_local": ip_local(),
                "porta_video": 1984,
                "porta_config": PORTA_CONFIG,
            })
        else:
            self._texto("Não encontrado", 404)

    def do_POST(self):
        if self.path == "/api/cameras":
            tamanho = int(self.headers.get("Content-Length", 0))
            dados = json.loads(self.rfile.read(tamanho))
            cams = carregar_cameras()
            cid = dados.get("id")
            if not cid:
                cid = proximo_id(cams, dados["tipo"], dados.get("canal"))
            nova = {
                "id": cid,
                "tipo": dados["tipo"],
                "nome": dados["nome"],
                "ip": dados["ip"],
                "usuario": dados["usuario"],
            }
            if dados["tipo"] == "dvr":
                nova["canal"] = dados.get("canal", 1)
            if dados.get("senha"):
                nova["_senha_nova"] = dados["senha"]

            indice = next((i for i, c in enumerate(cams) if c["id"] == cid), None)
            if indice is not None:
                cams[indice] = nova  # edição: mantém a posição na ordem
            else:
                cams.append(nova)  # nova câmera: vai pro fim da lista
            aplicar(cams)
            salvar_cameras([{k: v for k, v in c.items() if k != "_senha_nova"} for c in cams])
            self._json({"ok": True})
        elif self.path == "/api/ordem":
            tamanho = int(self.headers.get("Content-Length", 0))
            ordem_ids = json.loads(self.rfile.read(tamanho))  # lista de ids na nova ordem
            cams = carregar_cameras()
            por_id = {c["id"]: c for c in cams}
            cams_ordenadas = [por_id[cid] for cid in ordem_ids if cid in por_id]
            # qualquer câmera que não veio na lista (não deveria acontecer) fica no fim
            cams_ordenadas += [c for c in cams if c["id"] not in ordem_ids]
            salvar_cameras(cams_ordenadas)
            gerar_viewer(cams_ordenadas)  # só reordena a tela, não mexe no go2rtc
            self._json({"ok": True})
        else:
            self._texto("Não encontrado", 404)

    def do_DELETE(self):
        m = re.match(r"^/api/cameras/(\w+)$", self.path)
        if m:
            cid = m.group(1)
            cams = [c for c in carregar_cameras() if c["id"] != cid]
            salvar_cameras(cams)
            aplicar(cams)
            self._json({"ok": True})
        else:
            self._texto("Não encontrado", 404)


def main():
    garantir_go2rtc()
    cams_salvas = carregar_cameras()
    if cams_salvas:
        aplicar(cams_salvas)  # regera cameras.generated.js + go2rtc.yaml e sobe o go2rtc
    ip = ip_local()
    print("")
    print("=== Configurar câmeras ===")
    print(f"Nesta máquina:  http://localhost:{PORTA_CONFIG}/")
    print(f"Na rede (TV/celular também acessa a config se quiser): http://{ip}:{PORTA_CONFIG}/")
    print("Ctrl+C para encerrar (derruba o go2rtc junto).")
    print("")
    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA_CONFIG), Handler)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        parar_go2rtc()


if __name__ == "__main__":
    main()
