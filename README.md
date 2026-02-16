# ALDI Data

## Progress endpoint quick check

Use this one-liner to watch operation progress updates in real time (replace `<op_id>`):

```bash
watch -n 0.2 "curl -s https://mobile.stanway.me/api/progress/<op_id> | jq"
```
