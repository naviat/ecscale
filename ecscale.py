#!/bin/python
import boto3
import datetime

SCALE_IN_CPU_TH = 30
SCALE_IN_MEM_TH = 60
FUTURE_MEM_TH = 75
ECS_AVOID_STR = 'awseb'


def clusters(ecsClient):
    # Returns an iterable list of cluster names
    response = ecsClient.list_clusters()
    if not response['clusterArns']:
        print 'No ECS cluster found'
        exit

    return [cluster for cluster in response['clusterArns'] if ECS_AVOID_STR not in cluster]


def cluster_memory_reservation(cwClient, clusterName):
    # Return cluster mem reservation average per minute cloudwatch metric
    try:
        response = cwClient.get_metric_statistics( 
            Namespace='AWS/ECS',
            MetricName='MemoryReservation',
            Dimensions=[
                {
                    'Name': 'ClusterName',
                    'Value': clusterName
                },
            ],
            StartTime=datetime.datetime.utcnow() - datetime.timedelta(seconds=120),
            EndTime=datetime.datetime.utcnow(),
            Period=60,
            Statistics=['Average']
        )
        return response['Datapoints'][0]['Average']

    except Exception:
        print 'Could not retrieve mem reservation for {}'.format(clusterName)


def find_asg(clusterName, asgClient):
    # Returns auto scaling group resourceId based on name
    response = asgClient.describe_auto_scaling_groups()
    for asg in response['AutoScalingGroups']:
        for tag in asg['Tags']:
            if tag['Key'] == 'Name':
                if tag['Value'].split(' ')[0] == clusterName:
                    return tag['ResourceId']

    else:
        print 'auto scaling group for {} not found. exiting'.format(clusterName)


def ec2_avg_cpu_utilization(clusterName, asgclient, cwclient):
    asg = find_asg(clusterName, asgclient)
    response = cwclient.get_metric_statistics( 
        Namespace='AWS/EC2',
        MetricName='CPUUtilization',
        Dimensions=[
            {
                'Name': 'AutoScalingGroupName',
                'Value': asg
            },
        ],
        StartTime=datetime.datetime.utcnow() - datetime.timedelta(seconds=120),
        EndTime=datetime.datetime.utcnow(),
        Period=60,
        Statistics=['Average']
    )
    return response['Datapoints'][0]['Average']


def empty_instances(clusterArn, activeContainerDescribed):
    # returns a object of empty instances in cluster
    instances = []
    empty_instances = {}

    for inst in activeContainerDescribed['containerInstances']:
        if inst['runningTasksCount'] == 0 and inst['pendingTasksCount'] == 0:
            empty_instances.update({inst['ec2InstanceId']: inst['containerInstanceArn']})

    return empty_instances


def draining_instances(clusterArn, drainingContainerDescribed):
    # returns an object of draining instances in cluster
    instances = []
    draining_instances = {} 

    for inst in drainingContainerDescribed['containerInstances']:
        draining_instances.update({inst['ec2InstanceId']: inst['containerInstanceArn']})

    return draining_instances


def terminate_decrease(instanceId, asgClient):
    # terminates an instance and decreases the desired number in its auto scaling group
    # [ only if desired > minimum ]
    try:
        response = asgClient.terminate_instance_in_auto_scaling_group(
            InstanceId=instanceId,
            ShouldDecrementDesiredCapacity=True
        )
        print response['Activity']['Cause']

    except Exception as e:
        print 'Termination failed: {}'.format(e)


def scale_in_instance(clusterArn, activeContainerDescribed):
    # iterates over hosts, finds the least utilized:
    # The most under-utilized memory and minimum running tasks
    # return instance obj {instanceId, runningInstances, containerinstanceArn}
    instanceToScale = {'id': '', 'running': 0, 'freemem': 0}
    for inst in activeContainerDescribed['containerInstances']:
        for res in inst['remainingResources']:
            if res['name'] == 'MEMORY':
                if res['integerValue'] > instanceToScale['freemem']:
                    instanceToScale['freemem'] = res['integerValue']
                    instanceToScale['id'] = inst['ec2InstanceId']
                    instanceToScale['running'] = inst['runningTasksCount']
                    instanceToScale['containerInstanceArn'] = inst['containerInstanceArn']
                    
                elif res['integerValue'] == instanceToScale['freemem']:
                    # Two instances with same free memory level, choose the one with less running tasks
                    if inst['runningTasksCount'] < instanceToScale['running']:
                        instanceToScale['freemem'] = res['integerValue']
                        instanceToScale['id'] = inst['ec2InstanceId']
                        instanceToScale['running'] = inst['runningTasksCount'] 
                        instanceToScale['containerInstanceArn'] = inst['containerInstanceArn']
                break

    print 'Scale candidate: {}'.format(instanceToScale)
    return instanceToScale

    
def running_tasks(instanceId, containerDescribed):
    # return a number of running tasks on a given ecs host
    for inst in containerDescribed['containerInstances']:
        if inst['ec2InstanceId'] == instanceId:
            return int(inst['runningTasksCount']) + int(inst['pendingTasksCount']) 
    
    else:
        print 'Instance not found'


def drain_instance(containerInstanceId, ecsClient, clusterArn):
    # put a given ec2 into draining state
    try:
        response = ecsClient.update_container_instances_state(
            cluster=clusterArn,
            containerInstances=[containerInstanceId],
            status='DRAINING'
        )
        print 'Done draining'            

    except Exception as e:
        print 'Draining failed: {}'.format(e) 


def future_reservation(activeContainerDescribed, clusterMemReservation):
    # If the cluster were to scale in an instance, calculate the effect on mem reservation
    # return cluster_mem_reserve*num_of_ec2 / num_of_ec2-1
    numOfEc2 = len(activeContainerDescribed['containerInstances'])
    if numOfEc2 > 1:
        futureMem = (clusterMemReservation*numOfEc2) / (numOfEc2-1)
    else:
        print 'Less than 1 instance, cannot calculate future reservation'
        return 100

    print 'Current reservation vs Future: {} : {}'.format(clusterMemReservation, futureMem)
    return futureMem


def main():
    ecsClient = boto3.client('ecs')
    cwClient = boto3.client('cloudwatch')
    asgClient = boto3.client('autoscaling')
    clusterList = clusters(ecsClient)

    for cluster in clusterList:
        ## Retrieve container instances data: ##
        clusterName = cluster.split('/')[1]
        print '*** {} ***'.format(clusterName)
        activeContainerInstances = ecsClient.list_container_instances(cluster=cluster, status='ACTIVE')
        clusterMemReservation = cluster_memory_reservation(cwClient, clusterName)
        
        if activeContainerInstances['containerInstanceArns']:
            activeContainerDescribed = ecsClient.describe_container_instances(cluster=cluster, containerInstances=activeContainerInstances['containerInstanceArns'])
        else: 
            print 'No active instances in cluster'
            continue 
        drainingContainerInstances = ecsClient.list_container_instances(cluster=cluster, status='DRAINING')
        if drainingContainerInstances['containerInstanceArns']: 
            drainingContainerDescribed = ecsClient.describe_container_instances(cluster=cluster, containerInstances=drainingContainerInstances['containerInstanceArns'])
            drainingInstances = draining_instances(cluster, drainingContainerDescribed)
        else:
            drainingContainerDescribed = []
            drainingInstances = {}
        emptyInstances = empty_instances(cluster, activeContainerDescribed)
        ######### End of data retrieval #########

        if (future_reservation(activeContainerDescribed, clusterMemReservation) < FUTURE_MEM_TH): 
            if emptyInstances.keys():
                for instanceId, containerInstId in emptyInstances.iteritems():
                    print 'I am draining {}'.format(instanceId)
                    drain_instance(containerInstId, ecsClient, cluster)

            if (clusterMemReservation < SCALE_IN_MEM_TH): 
                if (ec2_avg_cpu_utilization(clusterName, asgClient, cwClient) < SCALE_IN_CPU_TH):
                # cluster hosts can be scaled in
                    instanceToScale = scale_in_instance(cluster, activeContainerDescribed)['containerInstanceArn']
                    print 'Going to scale {}'.format(instanceToScale)
                    drain_instance(instanceToScale, ecsClient, cluster)


        if drainingInstances.keys():
            for instanceId, containerInstId in drainingInstances.iteritems():
                if not running_tasks(instanceId, drainingContainerDescribed):
                    print 'Terminating draining instance with no containers {}'.format(instanceId)
                    terminate_decrease(instanceId, asgClient)
                else:
                    print 'Draining instance not empty'


if __name__ == '__main__':
    main()
