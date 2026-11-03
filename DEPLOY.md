# Deploy

Status: **planejado, ainda não implementado.** Este documento registra a decisão e o passo a passo para retomarmos depois.

## Decisão de hospedagem

| Opção | Veredito | Motivo |
|---|---|---|
| **VPS própria com systemd + nginx** | **Escolhida** | Processo persistente: cache em memória, threads de refresh e warm-up funcionam como em desenvolvimento. |
| Fly.io / Railway / Render | Alternativa | Mesmo modelo (container persistente); útil se a VPS não estiver disponível. |
| GitHub Pages | Descartado | Só serve estáticos. O backend é necessário: o ingresso.com provavelmente não permite CORS (não verificado), e o cache e o `/ocupacao` em lote vivem no servidor. |
| Vercel (serverless) | Descartado | Cache em memória e disco não persistem entre invocações, a thread de stale-while-revalidate morre, e o fan-out de cerca de 60 requests no cold start pode estourar o timeout. |

A VPS já tem Python, nginx, Docker e Java. **Java não é usado.** Nada além do que já existe é necessário.

## Arquitetura

```
Internet → nginx (80/443, rate limit, HTTPS) → uvicorn 127.0.0.1:8000 (1 worker) → ingresso.com
                                                   └─ cache: memória + ~/.cache/cinema-fortaleza/
```

O FastAPI já serve o frontend (`/`, `/static`), faz gzip e envia `Cache-Control`. O nginx não precisa tratar isso.

## Opção A — systemd + venv (recomendada)

```bash
sudo useradd -r -m -s /usr/sbin/nologin cinema
sudo -u cinema git clone https://github.com/LucasEmanuell/cinema.git /home/cinema/app
cd /home/cinema/app
sudo -u cinema python3 -m venv .venv
sudo -u cinema .venv/bin/pip install -r requirements.txt
```

`/etc/systemd/system/cinema.service`:

```ini
[Unit]
Description=cinema br
After=network.target

[Service]
User=cinema
WorkingDirectory=/home/cinema/app
ExecStart=/home/cinema/app/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now cinema
sudo systemctl status cinema
```

## Opção B — Docker

Equivalente à A, com isolamento extra e sem ganho de performance. Um `Dockerfile` mínimo:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

Publicar a porta só em loopback (`-p 127.0.0.1:8000:8000`) e montar um volume para o cache (`-v cinema-cache:/root/.cache`).

## nginx

```nginx
limit_req_zone $binary_remote_addr zone=cinema:10m rate=10r/s;

server {
    listen 80;
    server_name seu.dominio.com;          # ou "_" se for só por IP

    location / {
        limit_req zone=cinema burst=30 nodelay;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

HTTPS (exige domínio): `sudo certbot --nginx`.
Sem domínio: acesso por `http://IP`, ou Cloudflare Tunnel / Tailscale para ter HTTPS sem abrir portas.

## Pontos que importam

- **Um único worker do uvicorn.** Não use `--workers N`: o cache em memória é por processo, então vários workers dividiriam o cache e repetiriam o fan-out. As rotas são síncronas e rodam em threadpool; um processo atende uso pessoal ou poucos usuários.
- **Cache em disco** fica em `~cinema/.cache/cinema-fortaleza/` e sobrevive a reinícios. O `lifespan` do app aquece Fortaleza ao subir.
- **Rate limit no nginx** protege o ingresso.com se o endereço for público (o `/ocupacao` dispara até 8 requests em paralelo por chamada).
- **Firewall:** liberar só 80 e 443 (`ufw allow 'Nginx Full'`). A porta 8000 fica em `127.0.0.1`.
- **Atualizar:** `cd /home/cinema/app && sudo -u cinema git pull && sudo systemctl restart cinema`.
- **Logs:** `journalctl -u cinema -f`. Avisos de mudança de schema da API: `~cinema/.cache/cinema-fortaleza/schema_warnings.log`.

## Pendências para retomar

- [ ] Definir se haverá **domínio** (HTTPS via certbot) ou acesso só por IP / túnel.
- [ ] Definir se o uso é **pessoal ou público** (se público, manter o rate limit e considerar limite por rota no `/ocupacao`).
- [ ] Escolher entre a **Opção A** e a **B**.
- [ ] Criar no repositório os arquivos de deploy: `deploy/cinema.service`, `deploy/nginx.conf`, `deploy/deploy.sh` (ou `Dockerfile`).
- [ ] Validar na VPS o cold start real de Fortaleza e o tempo de resposta de `/filmes` e `/ocupacao`.
- [ ] Decidir se vale restringir `allow_origins` do CORS (hoje `*`) quando houver domínio fixo.
