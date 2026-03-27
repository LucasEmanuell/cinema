# cinema-br

Sessões de cinema em qualquer cidade do Brasil — mais rápido que o app e o site.

Funciona como **CLI** e como **API web** (para frontends).

## Instalação

```bash
pip install requests rich fastapi uvicorn
```

## Estrutura do projeto

```
cinema/
├── core.py          ← cache, requisições HTTP, chamadas à API ingresso.com, helpers
├── cli.py           ← display (rich), comandos, argparse
├── app.py           ← API web (FastAPI)
├── cinema.py        ← entrypoint do CLI (8 linhas, chama cli.py)
├── README.md        ← este arquivo
├── API_RESEARCH.md  ← documentação completa dos endpoints da ingresso.com
├── APP_DESIGN.md    ← decisões de arquitetura e features planejadas
└── DATA_STRATEGY.md ← análise sobre o que vale armazenar e como
```

---

## CLI

```bash
python cinema.py <comando> [opções]
```

### `filmes` — O que está em cartaz

```bash
python cinema.py filmes
python cinema.py filmes --cidade "recife"
```

Lista os filmes em cartaz na cidade indicada, ordenados por número de salas nacionais. Mostra classificação indicativa, duração e data de estreia. Padrão: Fortaleza.

**Opções:**

| Flag | O que faz |
|---|---|
| `--cidade CIDADE` | Cidade alvo (parcial, sem acento). Ex: `"recife"`, `"sao paulo"`, `"belo horizonte"`. |

---

### `sessoes` — Sessões de um filme

```bash
python cinema.py sessoes "<título>"
```

Busca por título parcial, sem acento e case-insensitive ("panico" encontra "Pânico 7"). Mostra todos os cinemas e sessões na cidade indicada.

Sem `--data`, usa o dia de hoje. Se não houver sessões hoje (ex: tarde da noite), avança automaticamente para a próxima data disponível.

**Opções:**

| Flag | O que faz |
|---|---|
| `--cidade CIDADE` | Cidade alvo (padrão: Fortaleza). Parcial, sem acento. Ex: `"recife"`, `"sao paulo"`. |
| `--data DATA` | Data alvo. Aceita `YYYY-MM-DD`, `amanha`, `+1`, `+2`… |
| `--teatro NOME` | Filtra por nome do cinema (parcial, sem acento). Ex: `"via sul"`, `"iguatemi"`. |
| `--hora HH:MM` | Filtra por horário. Ex: `"20"` mostra só sessões das 20h. |
| `--precos` | Mostra taxa de serviço separada. Alerta quando a taxa é desproporcional para quem paga meia. |
| `--ocupacao` | Mostra lotação em tempo real de cada sessão (requisições extras). |
| `--assentos` | Mostra mapa de assentos inline. Recomendado usar com `--teatro`. |
| `--numeros` | No mapa de assentos, exibe o número do assento no lugar do símbolo. |
| `--ids` | Exibe `session_id` e `section_id` de cada sessão (modo desenvolvedor). |

**Exemplos:**

```bash
# Sessões de hoje em Fortaleza (auto-avança se não houver)
python cinema.py sessoes "super mario"

# Outra cidade
python cinema.py sessoes "super mario" --cidade "recife"
python cinema.py sessoes "super mario" --cidade "sao paulo"

# Amanhã ou daqui a 2 dias
python cinema.py sessoes "mario" --data amanha
python cinema.py sessoes "mario" --data +2

# Numa data específica
python cinema.py sessoes "super mario" --data 2026-04-05

# Filtrar por cinema e horário
python cinema.py sessoes "panico" --teatro "via sul" --hora 20

# Ver mapa de assentos inline
python cinema.py sessoes "panico" --teatro "via sul" --assentos
python cinema.py sessoes "panico" --teatro "via sul" --assentos --numeros

# Comparar preço online vs bilheteria
python cinema.py sessoes "velhos bandidos" --precos

# Ver qual sessão está menos lotada
python cinema.py sessoes "super mario" --ocupacao

# Obter IDs para usar com o comando assentos
python cinema.py sessoes "panico" --teatro "benfica" --ids
```

---

### `assentos` — Mapa de assentos de uma sessão

```bash
python cinema.py assentos <session_id> <section_id>
python cinema.py assentos <session_id> <section_id> --numeros
```

Obtenha os IDs com `sessoes --ids`:

```bash
# 1. Descubra os IDs
python cinema.py sessoes "panico" --teatro "via sul" --ids

# 2. Use-os para ver o mapa
python cinema.py assentos 84262624 5472265
```

**Opções:**

| Flag | O que faz |
|---|---|
| `--numeros` | Exibe o número de cada assento no lugar do símbolo. |

Mostra:
- Número total de assentos, disponíveis e ocupados
- Porcentagem de lotação com barra colorida (verde < 50%, amarelo < 80%, vermelho ≥ 80%)
- Mapa visual da sala com posição real dos assentos e corredores
- Banner `TELA` alinhado com a geometria real da sala

Legenda padrão: `○` livre · `●` ocupado · `◇` SuperSeat · `()` namorados · `W` acessível · `O` obeso

---

## API web

```bash
uvicorn app:app --reload
```

Disponível em `http://localhost:8000`. Documentação interativa em `http://localhost:8000/docs`.

A API compartilha o mesmo cache do CLI (`~/.cache/cinema-fortaleza/`), então respostas já consultadas no terminal são instantâneas na API e vice-versa.

### Endpoints

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/cidades` | Lista todos os estados e cidades disponíveis |
| `GET` | `/filmes` | Filmes em cartaz (aceita `?cidade=`) |
| `GET` | `/sessoes/{filme}` | Sessões de um filme (aceita `?cidade=`, `?data=`, `?teatro=`, `?hora=`) |
| `GET` | `/sessoes/{filme}/datas` | Datas disponíveis para um filme (aceita `?cidade=`) |
| `GET` | `/tickets/{session_id}/{section_id}` | Preços com taxa de serviço separada |
| `GET` | `/assentos/{session_id}/{section_id}` | Mapa de assentos completo |

O parâmetro `filme` e `cidade` aceitam texto parcial sem acento, igual ao CLI. Cidade padrão: Fortaleza.

**Exemplos:**
```bash
curl "http://localhost:8000/cidades"
curl "http://localhost:8000/filmes?cidade=recife"
curl "http://localhost:8000/sessoes/panico?cidade=sao+paulo&data=amanha"
curl "http://localhost:8000/sessoes/panico?teatro=via+sul&data=amanha"
curl "http://localhost:8000/assentos/84262624/5472265"
```

---

## Como funciona

### Fontes de dados

Duas APIs públicas da ingresso.com, descobertas por engenharia reversa (o Swagger oficial está vazio):

| Base URL | Usado para |
|---|---|
| `https://api-content.ingresso.com/v0` | Filmes, cinemas, sessões/programação |
| `https://api.ingresso.com/v1` | Preços detalhados, mapa de assentos |

Todas as requisições usam `?partnership=ingresso.com`. Sem autenticação para leitura.

### Cache local

Respostas salvas em `~/.cache/cinema-fortaleza/` como JSON com TTL. Compartilhado entre CLI e API.

| Dado | TTL | Motivo |
|---|---|---|
| Cidades | 24 horas | Raramente muda |
| Lista de filmes | 1 hora | Muda quando estreia filme novo |
| Sessões / programação | 15 min | Estável após ser publicada |
| Preços (tickets) | 1 hora | Raramente muda durante o dia |
| Mapa de assentos | 5 min | Muda conforme ingressos são vendidos |

### Fluxo de dados

**`filmes` / `GET /filmes`**
```
GET /v0/events?cityId={id}&isPlaying=true&partnership=ingresso.com
  → filtra isPlaying=true
  → ordena por countIsPlaying (número de salas nacional)
```

**`sessoes` / `GET /sessoes/{filme}`**
```
1. GET /v0/states  (cached 24h)
   → resolve cidade por nome parcial → city_id

2. GET /v0/events?cityId={id}  (cached)
   → busca o filme por título parcial (tolerante a acentos)

3. Resolve a data: hoje / amanha / +N / YYYY-MM-DD
   → se não houver sessões hoje, auto-avança para a próxima data disponível

4. GET /v0/sessions/city/{id}/event/{id}/.../groupBy/sessionType?date=D
   → lista de cinemas → sessionTypes[] → sessions[]
   → filtra por teatro e hora se fornecidos

5. (precos / GET /tickets) GET /v1/sessions/{id}/sections/{sectionId}/tickets
   → price (bilheteria), service (taxa online), total
   → detecta Meia-Entrada com taxa desproporcional (> 20%)

6. (ocupacao/assentos / GET /assentos) GET /v1/sessions/{id}/sections/{sectionId}/seats
   → totalSeats, status por assento, geometria da sala
```

### Sobre os preços

A taxa de serviço da ingresso.com é **sempre 14% do preço da Inteira**, aplicada como valor fixo por sessão — independente do tipo de ingresso:

- **Inteira:** R$ 44,00 + R$ 6,16 taxa = R$ 50,16 (14%)
- **Meia-Entrada:** R$ 22,00 + R$ 6,16 taxa = R$ 28,16 (**28%**)

Para quem paga meia (estudantes, idosos, PCD), a taxa online equivale ao próprio desconto. O CLI avisa e a API inclui o campo `meia_fee_warning: true`.

**Exceção:** UCI tem tickets "UNIQUE MEIA ENTRADA" com taxa proporcional de 14%. Nesses casos nenhum aviso é emitido.

---

## Escopo

Este projeto é uma **ferramenta de consulta**. Ele lê informações públicas da ingresso.com e as exibe de forma mais rápida e acessível. Não realiza pagamentos, não cria contas, não compra ingressos e não tem afiliação com a ingresso.com.

Para comprar, use o site ou app da ingresso.com ou a bilheteria do cinema.

## Limitações conhecidas

- **`filmes`** lista filmes nacionais em cartaz. Alguns podem não ter sessões na cidade consultada; nesse caso `sessoes` informa.
- **`session.price` na saída padrão** pode ser o preço de SuperSeat quando a sala tem recliners — UCI Iguatemi é o caso conhecido. Use `--precos` ou `GET /tickets` para o breakdown real.
- **Sessões de hoje** às vezes retornam vazio se o horário já passou — o projeto avança automaticamente para a próxima data disponível.

---

## Melhorias futuras

- [ ] Comando `cinemas` para listar cinemas de uma cidade com endereço e salas
- [ ] Flag `--formato` para filtrar sessões por tipo (IMAX, VIP, etc.)
- [ ] Flag `--menor-preco` para ordenar por preço
- [ ] Tracking de ocupação para sessões consultadas (SQLite, ver `DATA_STRATEGY.md`)
