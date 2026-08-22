#!/usr/bin/env bash
set -euo pipefail
# Run ON hp-15-bw036nr-d (192.168.10.40) when online — renames partition PCC -DATA -> pcc-data and updates sharing
echo "=== HP-15-BW036NR-D partition rename pcc-data ==="
# Detect external drive (likely /dev/sdb1 or /dev/sda1 with LABEL PCC -DATA)
sudo blkid 2>&1 | grep -i "PCC" | head -n 20
DEV=$(sudo blkid -o device -t LABEL="PCC -DATA" 2>&1 | head -n1)
if [ -z "$DEV" ]; then DEV=$(sudo blkid -o device -t LABEL="PCC-DATA" 2>&1 | head -n1); fi
if [ -z "$DEV" ]; then DEV=$(sudo blkid -o device -t LABEL="pcc-data" 2>&1 | head -n1); fi
if [ -z "$DEV" ]; then echo "No PCC partition found via blkid, trying lsblk"; lsblk -o NAME,LABEL,FSTYPE,SIZE,MOUNTPOINT 2>&1 | head -n 50; DEV="/dev/sdb1"; fi
echo "Device: $DEV"
FSTYPE=$(lsblk -no FSTYPE "$DEV" 2>&1 | head -n1 | tr -d ' ')
echo "FSTYPE: $FSTYPE"
case "$FSTYPE" in
  ext4) sudo e2label "$DEV" pcc-data 2>&1 | head -n 5; echo "e2label done" ;;
  ntfs) sudo ntfslabel "$DEV" pcc-data 2>&1 | head -n 5 ;;
  vfat|exfat) sudo fatlabel "$DEV" pcc-data 2>&1 | head -n 5 ;;
  *) echo "Unknown FSTYPE $FSTYPE, trying e2label"; sudo e2label "$DEV" pcc-data 2>&1 | head -n 20 || true ;;
esac
sudo blkid -o value -s LABEL "$DEV" 2>&1 | head -n 5
# Update hostname if still L
if grep -q "HP-15-BW036NR-L" /etc/hostname 2>&1; then
  echo "HP-15-BW036NR-L" | sudo tee /etc/hostname 2>&1 | head -n 5
  sudo sed -i "s/HP-15-BW036NR-L/HP-15-BW036NR-D/g" /etc/hosts 2>&1 | head -n 5
  sudo hostnamectl set-hostname HP-15-BW036NR-D 2>&1 | head -n 5
  echo "hostname updated to D"
fi
cat /etc/hostname 2>&1 | head -n 5
# Update samba share
if grep -q "\[pc-data\]" /etc/samba/smb.conf 2>&1; then
  sudo sed -i "s/\[pc-data\]/[pcc-data]/g" /etc/samba/smb.conf 2>&1 | head -n 5
  echo "samba share renamed pc-data -> pcc-data"
fi
if grep -q "PCC" /etc/samba/smb.conf 2>&1; then
  sudo sed -i "s|PCC.*DATA|pcc-data|g" /etc/samba/smb.conf 2>&1 | head -n 5
  sudo sed -i "s|PCC\\040-DATA|pcc-data|g" /etc/samba/smb.conf 2>&1 | head -n 5
fi
grep -A3 "pcc-data" /etc/samba/smb.conf 2>&1 | head -n 20
# Ensure mount point
sudo mkdir -p /media/a2gutierrezmora/pcc-data 2>&1 | head -n 5
# Unmount old if mounted
if mount 2>&1 | grep -q "PCC"; then sudo umount "/media/a2gutierrezmora/PCC -DATA" 2>&1 | head -n 5 || true; fi
# Remount via udisks or mount
sudo systemctl restart smbd nmbd 2>&1 | head -n 20 || sudo service smbd restart 2>&1 | head -n 20
echo "Done — reboot recommended, then verify lsblk LABEL=pcc-data and smbclient -L localhost"
