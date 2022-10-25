import jax
import jax.numpy as jnp
import flax.linen as nn
from flax.training import checkpoints, train_state
import optax



def encoding_func(x, L):
    encoded_array = [x]
    for i in range(L):
        encoded_array.extend([jnp.sin(2. ** i * jnp.pi * x), jnp.cos(2. ** i * jnp.pi * x)])
    return jnp.concatenate(encoded_array, -1)

def render(model_func, params, origin, direction, key, near, far, num_samples, L_position, rand):
    t = jnp.linspace(near, far, num_samples) 

    if rand: 
        random_shift = jax.random.uniform(key, (origin.shape[0], origin.shape[1], num_samples)) * (far-near)/num_samples  
        t = t+ random_shift 
    else:
        t = jnp.broadcast_to(t, (origin.shape[0], origin.shape[1], num_samples))

    points = origin[..., jnp.newaxis, :] + t[..., jnp.newaxis] * direction[..., jnp.newaxis, :]
    points = jnp.squeeze(points)
    points_flatten = points.reshape((-1, 3))
    encoded_x = encoding_func(points_flatten, L_position)
    
    rgb_array, opacity_array = [], []
    for _cc in range(0, encoded_x.shape[0], 4096*20):
        rgb, opacity = model_func.apply(params, encoded_x[_cc:_cc + 4096*20]) 
        rgb_array.append(rgb)
        opacity_array.append(opacity)
    
    rgb = jnp.concatenate(rgb_array, 0)
    opacity = jnp.concatenate(opacity_array, 0)
    
    rgb =rgb.reshape((points.shape[0], points.shape[1], num_samples, 3))
    opacity =opacity.reshape((points.shape[0], points.shape[1], num_samples, 1))

    rgb = jax.nn.sigmoid(rgb)
    opacity = jax.nn.relu(opacity) 
   
    t_delta = t[...,1:] - t[...,:-1]
    t_delta = jnp.concatenate([t_delta, jnp.broadcast_to(jnp.array([1e10]),   [points.shape[0], points.shape[1], 1])], 2)

    
    T_i = jnp.cumsum(jnp.squeeze(opacity) * t_delta + 1e-10, -1)   
    T_i = jnp.insert(T_i, 0, jnp.zeros_like(T_i[...,0]),-1)
    T_i = jnp.exp(-T_i)[..., :-1]
     
    c_array = T_i[..., jnp.newaxis]*(1.-jnp.exp(-opacity*t_delta[..., jnp.newaxis])) * rgb 
    c_sum =jnp.sum(c_array, -2)

    return c_sum 


def get_model(L_position):
    class Model(nn.Module):
    
      @nn.compact
      def __call__(self, z):
        input = z
        z = nn.Dense(L_position*6+3, name='fc_in')(z)
        z = nn.relu(z)
    
        for i in range(8):
            z = nn.Dense(256, name=f'fc{i}')(z)
            z = nn.relu(z)
            if i == 4:
                z = jnp.concatenate([z, input], -1) 
    
            if i == 7: 
                d = nn.Dense(1, name='fcd2')(z)
    
        z = nn.Dense(128, name='fc_128')(z)
        
        z = nn.Dense(3, name='fc_f')(z)
        return z, d 
    
    model = Model()
    params = model.init(jax.random.PRNGKey(0), jnp.ones((1, L_position * 6 + 3)))
    return model, params

def get_grad(model, params, data, render):
    origins, directions, y_target, key = data
    def loss_func(params):
        image_pred = render(model, params, origins, directions, key)
        return jnp.mean((image_pred -  y_target) ** 2), image_pred

    (loss_val, image_pred), grads = jax.value_and_grad(loss_func, has_aux=True)(params)
    return loss_val, grads, image_pred


def get_patches_grads(grad_fn, params, data):
    # this function is implemented for GPUs with low memory
    origins, directions, y_targets, keys = data
    loss_array, grads_array, pred_train_array = jax.lax.map(
        lambda grad_input : \
            grad_fn(params, grad_input), (origins, directions, y_targets, keys)
        )
    grads = jax.tree_map(lambda x : jnp.mean(x, 0), grads_array)

    loss_val = jnp.mean(loss_array)
    return loss_val, grads, pred_train_array

def get_nerf_componets(config):
    model, params = get_model(config['L_position'])
    
    near = config['near'] 
    far = config['far'] 
    num_samples = config['num_samples'] 
    L_position = config['L_position']

    
    # render function for training with random sampling
    render_concrete = lambda model_func, params, origin, direction, key: \
        render(model_func, params, origin, direction, key, near, far, num_samples, L_position, True)
    
    # render function for evaluation
    render_concrete_eval = lambda model_func, params, origin, direction: \
        render(model_func, params, origin, direction, None, near, far, num_samples, L_position, False)
       
    grad_fn = jax.jit(lambda params, data: get_grad(model, params, data, render_concrete))

    if config['split_to_patches']: 
        grad_fn_entire = lambda params, data: get_grad(model, params, data, render_concrete)
        grad_fn = jax.jit(lambda params, data : get_patches_grads(grad_fn_entire, params, data))
    
    
    learning_rate = config['init_lr'] 
    
    # create train state
    tx = optax.adam(learning_rate)
    state = train_state.TrainState.create(apply_fn=model.apply,
                                        params=params,
                                        tx=tx)
    
    # load from ckpt
    if 'checkpoint_dir' in config:
        print(f'Loading checkpoint from : {config["checkpoint_dir"]}')
        #opt_state = checkpoints.restore_checkpoint(ckpt_dir=config['checkpoint_dir'], target=state)
 
    model_components = {
        'model': model,
        'render_eval_fn': render_concrete_eval,
        'grad_fn': grad_fn,
        'state': state,
    }

    return model_components

if __name__ == '__main__':
    get_model(100)
