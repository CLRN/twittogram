# Twittogram bot
Forwards twitter streams to Telegram.

Example of k8s deployment:
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: twittogram-pv-claim
spec:
  storageClassName: local-path
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: twittogram
  labels:
    app: twittogram
spec:
  replicas: 1
  selector:
    matchLabels:
      name: twittogram
  strategy:
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 1
    type: RollingUpdate
  template:
    metadata:
      labels:
        name: twittogram
        app: twittogram
    spec:
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: twittogram-pv-claim
      containers:
      - image: clrn/twittogram:main
        imagePullPolicy: Always
        volumeMounts:
          - name: data
            mountPath: /data
        env:
          - name: CHATS_PATH
            value: /data/chats.json
          - name: TELEGRAM_BOT_ID
            value: xxx
          - name: CONSUMER_KEY
            value: xxx
          - name: CONSUMER_SECRET
            value: xxx
        name: twittogram
      restartPolicy: Always

```