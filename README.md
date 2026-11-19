# 📷 LocalCam

Veja suas câmeras Tapo (e canais de DVR Hikvision/JFL) no PC, no celular e na TV da sua
casa — **tudo só na rede local**, sem app de terceiro, sem nuvem, sem conta, sem porta
aberta no roteador.

Nasceu porque os apps oficiais de câmera de segurança são, em geral, ruins: cheios de
anúncio, de assinatura, de recurso de nuvem que você não quer. Suas câmeras Tapo e seu
DVR já falam **RTSP** e **ONVIF** nativamente — isso é tudo que você precisa pra ter sua
própria central de câmeras, local, grátis e sem travar.

## O que isso faz

- Uma **tela de configuração** no navegador — adiciona câmera, usuário, senha, arrasta
  pra reordenar. Sem editar arquivo nenhum.
- Uma **grade de vídeo** ao vivo, baixa latência (WebRTC), que abre em qualquer
  navegador da sua rede — inclusive o da Smart TV.
- Zero dependência pra instalar: só **Python** (já vem no Windows 11/macOS recentes, ou
  é uma instalação de 2 minutos) — nenhuma biblioteca externa. O
  [go2rtc](https://github.com/AlexxIT/go2rtc) (o motor que converte RTSP pra WebRTC) é
  baixado sozinho, automaticamente, na primeira vez que faltar.
- Tudo roda **na sua máquina**, dentro da sua rede. Nada sai pra internet.

## Pré-requisitos

- **Python 3.9+** instalado ([python.org](https://www.python.org/downloads/)).
- Suas câmeras na mesma rede local do PC, com **RTSP habilitado**:
  - **Tapo:** app Tapo → câmera → ⚙️ Configurações → Avançado → **Conta da câmera** (crie
    usuário e senha — é diferente do login da sua conta TP-Link).
  - **DVR/NVR Hikvision ou JFL:** já vem com RTSP ligado por padrão; só precisa do
    usuário/senha de admin (ou de um usuário de visualização).

## Como usar

```bash
git clone https://github.com/SEU-USUARIO/LocalCam.git
cd LocalCam
python app.py
```

Isso abre `http://localhost:8766/` — a tela de configuração. Na primeira vez que
precisar, o script baixa o `go2rtc` sozinho (± 20 MB, só acontece uma vez).

No Windows, dá pra também só dar **dois cliques em `Iniciar.bat`**.

Na tela de configuração:
1. **Adicionar câmera** → escolha o tipo (Tapo ou canal de DVR), preencha nome, IP,
   usuário e senha.
2. **Salvar e aplicar** — grava a configuração e já sobe o motor de vídeo sozinho.
3. Arraste pela alcinha (⠿) pra reordenar como quiser na grade.

Depois disso, a grade de vídeo fica em `http://SEU-IP-LOCAL:1984/` — esse é o endereço
que você abre na TV, no celular, em qualquer aparelho da mesma rede Wi-Fi.

### Descobrir o IP das câmeras

Se você não sabe o IP de uma câmera Tapo: app Tapo → câmera → Configurações → Sobre. Pra
um DVR, geralmente está na etiqueta do aparelho ou no app que você já usa pra acessá-lo
(Hik-Connect e similares).

### Canais de DVR (Hikvision / JFL / rebrands)

Gravadores dessa família usam um padrão de caminho RTSP por canal:
`/Streaming/Channels/{canal}01` (qualidade alta) e `{canal}02` (qualidade baixa/leve) —
`101`/`102` é o canal 1, `201`/`202` o canal 2, e assim por diante. A tela de
configuração já monta isso sozinha, só peça o número do canal.

## Como funciona por dentro

```
app.py (porta 8766)          go2rtc.exe (portas 1984/8554/8555)
  tela de configuração   -->   grade de vídeo (WebRTC/HLS/MJPEG)
  escreve go2rtc.yaml     ->   lê o yaml, conecta via RTSP nas câmeras
  (sobe/reinicia o go2rtc automaticamente a cada mudança)
```

- `app.py` — único arquivo Python (biblioteca padrão, sem dependência), serve a tela de
  config e orquestra o processo do go2rtc.
- `go2rtc.yaml`, `cameras.json`, `web/cameras.generated.js` — gerados automaticamente a
  partir do que você cadastra na tela. **Nunca vão pro Git** (dados pessoais: IP, nome
  dos cômodos, credenciais).
- `web/` — a página da grade de vídeo. `video-rtc.js` e `video-stream.js` são do próprio
  projeto [go2rtc](https://github.com/AlexxIT/go2rtc) (MIT), usados sem modificação.

## Segurança e privacidade

- **Isso é uma ferramenta pra rede local.** Não exponha as portas (`1984`, `8554`,
  `8555`, `8766`) na internet nem configure redirecionamento de porta (*port forwarding*)
  no roteador — quem tiver a senha RTSP enxergaria suas câmeras de fora de casa.
- As senhas das câmeras ficam **só no seu `go2rtc.yaml` local**, nunca em texto plano em
  nenhum outro lugar, nunca enviadas a servidor nenhum.
- Se quiser acesso remoto de verdade (fora de casa), use VPN até a sua rede (WireGuard,
  Tailscale) — não abra porta.

## Solução de problemas

- **Vídeo preto / não conecta:** confira usuário e senha. Pra DVR, confirme o número do
  canal — alguns modelos usam `1`, `2`, `3`... outros já numeram diferente.
- **Sem áudio:** todo navegador silencia vídeo em autoplay por padrão (política do
  próprio navegador, não é bug). Clique no ícone de volume dentro do vídeo pra desmutar.
- **"Porta em uso" ao iniciar:** algum outro programa já está usando a porta 8766 ou
  1984 (ex: outro NVR instalado, como Agent DVR/iSpy). Edite `PORTA_CONFIG` no topo do
  `app.py` pra trocar a porta da tela de configuração.
- **TV não abre o vídeo:** o navegador da TV precisa suportar WebRTC. Em TVs mais
  antigas ou com navegador limitado, pode não funcionar — Android TV/Google TV
  costumam funcionar bem.

## Testado com

- TP-Link Tapo C200
- DVR JFL (linha com firmware baseado em Hikvision) — 4 canais

Deve funcionar com qualquer câmera/DVR que exponha RTSP, incluindo outros modelos Tapo e
a maioria dos gravadores Hikvision/Dahua e seus rebrands (Intelbras, JFL etc.) — se você
testar com outro modelo, um PR atualizando esta lista é bem-vindo.

## Licença

MIT — veja [LICENSE](LICENSE). Usa o [go2rtc](https://github.com/AlexxIT/go2rtc) (MIT)
como motor de streaming.
