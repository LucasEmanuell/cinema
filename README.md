# cinema.py

Sessões de cinema em Fortaleza — mais rápido que o app e o site.

## Instalação

```bash
pip install requests rich
```

## Uso

```
python cinema.py <comando> [opções]
```

---

## Comandos

### `filmes` — O que está em cartaz

```bash
python cinema.py filmes
```

Lista os filmes em cartaz em Fortaleza ordenados por número de salas nacionais (proxy de popularidade). Mostra classificação indicativa, duração e data de estreia.

---

### `sessoes` — Sessões de um filme

```bash
python cinema.py sessoes "<título>"
```

Busca por título parcial, sem acento e case-insensitive ("panico" encontra "Pânico 7"). Mostra todos os cinemas e sessões em Fortaleza.

Sem `--data`, usa o dia de hoje. Se não houver sessões hoje (ex: tarde da noite), avança automaticamente para a próxima data disponível.

**Opções:**

| Flag | O que faz |
|---|---|
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
# Sessões de hoje (auto-avança se não houver)
python cinema.py sessoes "super mario"

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

## Como o script funciona

### Fontes de dados

O script usa duas APIs públicas da ingresso.com, descobertas por engenharia reversa (o Swagger oficial está vazio):

| Base URL | Usado para |
|---|---|
| `https://api-content.ingresso.com/v0` | Filmes, cinemas, sessões/programação |
| `https://api.ingresso.com/v1` | Preços detalhados, mapa de assentos |

Todas as requisições usam `?partnership=ingresso.com`. Sem autenticação para leitura.

### Cache local

O script salva as respostas em `~/.cache/cinema-fortaleza/` como arquivos JSON com TTL:

| Dado | TTL | Motivo |
|---|---|---|
| Lista de filmes | 1 hora | Muda quando estreia filme novo |
| Sessões / programação | 15 min | Estável após ser publicada |
| Preços (tickets) | 1 hora | Raramente muda durante o dia |
| Mapa de assentos | 5 min | Muda conforme ingressos são vendidos |

Na prática: a primeira chamada vai à API, as seguintes são instantâneas até o TTL expirar.

### Fluxo de dados por comando

**`filmes`**
```
GET /v0/events?cityId=36&isPlaying=true&partnership=ingresso.com
  → filtra isPlaying=true
  → ordena por countIsPlaying (número de salas nacional)
  → mostra os 30 primeiros
```

**`sessoes`**
```
1. GET /v0/events?cityId=36&isPlaying=true  (cached)
   → busca o filme por título parcial (tolerante a acentos)

2. Resolve a data: hoje / amanha / +N / YYYY-MM-DD
   → se não houver sessões hoje, busca datas disponíveis e auto-avança

3. GET /v0/sessions/city/36/event/{id}/partnership/ingresso.com/groupBy/sessionType?date=D
   → retorna lista de cinemas, cada um com sessionTypes[]
   → cada sessionType tem sessions[] com: id, time, room, price, defaultSector
   → filtra por --teatro e --hora se fornecidos

4. (se --precos) GET /v1/sessions/{id}/sections/{sectionId}/tickets
   → retorna: price (bilheteria), service (taxa online), total
   → detecta Meia-Entrada com taxa desproporcional (28% vs 14% da Inteira)

5. (se --ocupacao ou --assentos) GET /v1/sessions/{id}/sections/{sectionId}/seats
   → conta seats com status "Available" vs total
   → calcula % de lotação; se --assentos, renderiza mapa inline
```

**`assentos`**
```
GET /v1/sessions/{sessionId}/sections/{sectionId}/seats
  → totalSeats, lines[].seats[].status
  → renderiza mapa ASCII
```

### Sobre os preços

A taxa de serviço da ingresso.com é **sempre 14% do preço da Inteira**, aplicada como valor fixo por sessão — independente do tipo de ingresso comprado:

- **Inteira:** R$ 44,00 + R$ 6,16 taxa = R$ 50,16 (14% sobre o ingresso)
- **Meia-Entrada:** R$ 22,00 + R$ 6,16 taxa = R$ 28,16 (**28% sobre o ingresso**)

Para quem paga meia (estudantes, idosos, PCD, jovens 15–29 de baixa renda), a taxa online representa 28% do valor do ingresso — o mesmo que o desconto da meia. O script avisa quando isso acontece para que o usuário decida se vale a pena comprar na bilheteria.

**Exceção:** UCI tem seus próprios tickets "UNIQUE MEIA ENTRADA" com taxa proporcional de 14%. Nesses casos nenhum aviso é exibido.

---

## Estrutura do projeto

```
cinema/
├── cinema.py        ← script principal
├── README.md        ← este arquivo
├── API_RESEARCH.md  ← documentação completa dos endpoints da API
├── APP_DESIGN.md    ← decisões de arquitetura e features planejadas
└── DATA_STRATEGY.md ← análise sobre o que vale armazenar e como
```

---

## Limitações conhecidas

- **`filmes` não filtra por Fortaleza** — mostra filmes nacionais em cartaz. Alguns podem não ter sessões em Fortaleza; nesse caso `sessoes` informa.
- **`session.price` na saída padrão** pode ser o preço de SuperSeat (recliner) quando a sala tem assentos premium — UCI Iguatemi é o caso conhecido. Use `--precos` para ver o breakdown real.
- **Sessões de hoje** às vezes retornam vazio se o horário já passou — o script avança automaticamente para a próxima data disponível.

---

## Melhorias futuras

- [ ] Comando `cinemas` para listar os 10 cinemas de Fortaleza com endereço e salas
- [ ] Flag `--formato IMAX` para filtrar sessões por tipo
- [ ] Flag `--menor-preco` para ordenar por preço
- [ ] Tracking de ocupação para sessões consultadas (SQLite, ver `DATA_STRATEGY.md`)
- [ ] Suporte a outras cidades via `--cidade` (a API já suporta, é só trocar o `cityId`)
