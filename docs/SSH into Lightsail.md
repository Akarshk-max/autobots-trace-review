# SSH into deployments (in remote machines)

## Autobots dev
```bash
ssh -i autobots-dev.pem ec2-user@54.68.98.149
```

## Autobots prod
```bash
ssh -i autobots-prod-1.pem ec2-user@52.37.109.196
```

Verify the *.pem file have narrow access  
- Check the permissions of pem files
```bash
ls -ltr | grep pem 
```
- Update Owner read permissions on file
```bash
chmod 400 autobots-prod-1.pem
```