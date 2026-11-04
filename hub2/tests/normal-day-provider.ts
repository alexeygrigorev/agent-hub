// Synthetic provider HTTP only; production Account collector parses and persists all evidence.
export const providerFetch:typeof fetch=async(input,init)=>{
 const url=new URL(typeof input==='string'?input:input instanceof URL?input.href:input.url);
 if(url.origin!=='https://chatgpt.com')throw new Error('unexpected synthetic provider origin');
 if(url.pathname==='/backend-api/wham/usage')return Response.json({rate_limit:{limit_reached:false,primary_window:{used_percent:20,reset_at:Math.floor(Date.now()/1000)+18000},secondary_window:{used_percent:30,reset_at:Math.floor(Date.now()/1000)+604800}}});
 if(url.pathname==='/backend-api/wham/rate-limit-reset-credits')return Response.json({credits:[]});
 throw new Error('unexpected synthetic provider endpoint');
};
