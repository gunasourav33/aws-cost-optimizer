# AWS Cost Optimizer

A collection of Boto3 scripts to identify and report on AWS cost waste. Built from real FinOps work — these scripts have found $140K+ in annual waste across various accounts.

## What it does

- **idle_ec2.py**: Finds EC2 instances with minimal CPU utilization over 2 weeks
- **unused_ebs.py**: Identifies unattached EBS volumes and orphaned snapshots
- **report.py**: Combines findings into a single cost report (HTML, JSON, or stdout table)

All scripts handle multi-region queries and support cross-account scanning via IAM role assumption.

## Quick start

### Prerequisites

```bash
pip install -r requirements.txt
```

### Single-account scan

```bash
# Scan all regions for idle EC2 instances
python scripts/idle_ec2.py

# Scan specific region
python scripts/idle_ec2.py --region us-east-1

# Find unused EBS volumes
python scripts/unused_ebs.py
```

### Cross-account scan

```bash
# Assume role in another account
export AWS_ROLE_ARN="arn:aws:iam::123456789012:role/CostOptmizerRole"
python scripts/idle_ec2.py
```

### Generate full report

```bash
python scripts/report.py --output-format json --output-file costs.json
python scripts/report.py --output-format table  # Print to stdout
```

## IAM permissions

Minimum permissions needed for each script:

**idle_ec2.py**:
```
ec2:DescribeInstances
ec2:DescribeRegions
cloudwatch:GetMetricStatistics
```

**unused_ebs.py**:
```
ec2:DescribeVolumes
ec2:DescribeSnapshots
ec2:DescribeInstances
```

**report.py**:
```
sts:GetCallerIdentity
```

For cross-account scanning, create a role in the target account with these permissions and an assume-role trust relationship to your scanning account.

## Known gotchas

- CloudWatch doesn't retain metrics older than 15 months, so some instances may show incomplete data
- Scheduled instances (Savings Plans, Reserved Instances) still show hourly costs; take that into account
- The cost estimates use on-demand pricing and don't account for savings plans or RI discounts
- Snapshots older than 90 days with no volume relationship are considered "orphaned" but double-check before deleting

## Sample output

```
Account: 123456789012

IDLE EC2 INSTANCES (avg CPU < 5% over 14 days)
instance_id       instance_type    avg_cpu    uptime_days    monthly_cost
i-0abc123def456    t3.large         2.1%       45             $28.54
i-1def456ghi789    m5.xlarge        1.3%       120            $142.80

UNUSED EBS VOLUMES
volume_id        size_gb    type    state        monthly_cost
vol-abc123       100        gp3     available    $8.50
vol-def456       250        io1     available    $60.00

Total estimated monthly waste: $239.84
```

## TODO

- [ ] Add RDS instance utilization analysis
- [ ] Generate RI/Savings Plan coverage report
- [ ] Integrate with AWS Organizations for automatic multi-account discovery
- [ ] Add support for Lambda cost analysis
- Slack integration for periodic reports
